import datetime
import json
try:
    import numpy as np  # optional
except Exception:  # pragma: no cover
    np = None
import os
import re
from typing import Optional, Dict, List, Any, Union, TypeVar, Generic, TYPE_CHECKING

if TYPE_CHECKING:
    from .models import Model

T = TypeVar('T', bound='Model')


class Q:
    """A Q object represents a complex query that can be combined with logical operators."""

    AND = "AND"
    OR = "OR"

    def __init__(self, **kwargs):
        # Convert enum values before storing
        converted_kwargs = self._convert_enum_kwargs(kwargs)
        self.children = list(converted_kwargs.items())
        self.connector = self.AND
        self.negated = False

    def _convert_enum_value(self, value):
        """Convert enum instances to their values for database queries."""
        import enum
        if isinstance(value, enum.Enum):
            return value.value
        return value

    def _convert_enum_kwargs(self, kwargs):
        """Convert enum values in kwargs dictionary."""
        return {key: self._convert_enum_value(value) for key, value in kwargs.items()}

    def _combine(self, other, conn):
        if not isinstance(other, Q):
            raise TypeError(f"Cannot combine Q object with {type(other)}")

        obj = Q()
        obj.connector = conn
        obj.children = [self, other]
        return obj

    def __and__(self, other):
        return self._combine(other, self.AND)

    def __or__(self, other):
        return self._combine(other, self.OR)

    def __invert__(self):
        obj = Q()
        obj.negated = not self.negated
        obj.children = [self]
        return obj




class QuerySet(Generic[T]):
    """QuerySet for database operations with chaining support.

    Note: This class can target any table. Pass the table name explicitly and
    optionally provide the set of JSON-capable columns. Defaults avoid
    hardcoding model-specific fields.
    """

    _json_fields = set()

    def __init__(self, model_cls):
        # Simple constructor that takes a Model class, like the old working code
        self.model = model_cls
        self.table = None  # Will be set based on provider type
        self._model_class = model_cls
        
        # Database access is deferred until needed
        self.db = None
        self.provider = None
        self._db_alias = None  # Database alias for .using()
        self._filters = []
        self._order_by = []
        self._limit_count = None
        self._offset_count = None
        self._select_fields = ["*"]
        self._values_mode = False
        self._values_fields = []
        self._values_flat = False
        self._result_cache = None
        self._search_order_by_rank = False
        
        # Will be determined when we get the provider
        self._is_sqlite = None
        
        # Derive JSON fields from the model
        derived_json = set()
        if hasattr(model_cls, '_neutronapi_fields_'):
            try:
                from .fields import JSONField
                for fname, f in model_cls._neutronapi_fields_.items():
                    if isinstance(f, JSONField):
                        derived_json.add(fname)
            except Exception:
                pass
        self._json_fields = derived_json
    
    def _get_table_identifier(self, provider):
        """Get the correct table identifier using the provider's method."""
        schema, table = self.model._get_parsed_table_name()
        return provider.get_table_identifier(schema, table)
    
    async def _get_provider(self):
        """Get and cache the database provider, and initialize dialect flags."""
        if hasattr(self, '_model_class'):
            from .connection import get_databases, DatabaseType
            db_manager = get_databases()
            alias = self._db_alias if self._db_alias else 'default'
            connection = await db_manager.get_connection(alias)
            provider = connection.provider
            # Cache provider for subsequent non-query-building paths (update/delete)
            self.provider = provider
            # Initialize dialect flag before any SQL construction
            if self._is_sqlite is None:
                self._is_sqlite = 'sqlite' in provider.__class__.__name__.lower()
            # Initialize table name based on provider type
            if self.table is None:
                self.table = self._get_table_identifier(provider)
            return provider
        else:
            # If provider already set externally, ensure dialect flag is set
            if self.provider and self._is_sqlite is None:
                self._is_sqlite = 'sqlite' in self.provider.__class__.__name__.lower()
            return self.provider

    def filter(self, *args, **kwargs) -> 'QuerySet':
        return self._add_filters(args, kwargs, negated=False)

    def exclude(self, *args, **kwargs) -> 'QuerySet':
        return self._add_filters(args, kwargs, negated=True)

    def _convert_enum_value(self, value):
        """Convert enum instances to their values for database queries."""
        import enum
        if isinstance(value, enum.Enum):
            return value.value
        return value

    def _convert_enum_kwargs(self, kwargs):
        """Convert enum values in kwargs dictionary."""
        return {key: self._convert_enum_value(value) for key, value in kwargs.items()}

    def _add_filters(self, args, kwargs, negated=False):
        qs = self._clone()
        q_objects = list(args)

        if kwargs:
            # Convert enum values before creating Q object
            converted_kwargs = self._convert_enum_kwargs(kwargs)
            q_objects.append(Q(**converted_kwargs))

        if not q_objects:
            return qs

        if negated:
            # Negate each Q object before combining
            q_objects = [~q for q in q_objects]

        # Add new filters to the list
        for q_obj in q_objects:
            qs._filters.append({'type': 'q_object', 'q_object': q_obj})
        return qs

    def order_by(self, *fields) -> 'QuerySet':
        qs = self._clone()
        qs._order_by = []
        for field in fields:
            desc = field.startswith('-')
            field_name = field[1:] if desc else field

            # Store the field path and direction for later processing
            # JSON field detection will be done at query build time when provider is available
            qs._order_by.append({
                'field': field_name,
                'direction': 'DESC' if desc else 'ASC'
            })
        return qs

    def _is_json_field_lookup(self, field_path: str) -> bool:
        """Check if a field path is a JSON field lookup."""
        parts = field_path.split('__')
        field_name = parts[0]
        return field_name in self._json_fields and len(parts) > 1

    def _build_json_order_expression(self, field_path: str) -> str:
        """Build JSON extraction expression for ordering."""
        parts = field_path.split('__')
        field_name = parts[0]
        json_path = parts[1:]

        if self._is_sqlite:
            json_path_str = f"$.{'.'.join(json_path)}"
            return f"json_extract({field_name}, '{json_path_str}')"
        else:
            # PostgreSQL JSON path
            json_path_str = '{' + ','.join(json_path) + '}'
            return f"{field_name}#>>'{json_path_str}'"

    def order_by_rank(self) -> 'QuerySet':
        """Order results by search rank when a search filter is present.

        - PostgreSQL: uses ts_rank(...)
        - SQLite with FTS5 configured: uses bm25(fts_table)
        - Otherwise: no-op (falls back to existing ordering)
        """
        qs = self._clone()
        qs._search_order_by_rank = True
        return qs

    def limit(self, count: int) -> 'QuerySet':
        qs = self._clone()
        qs._limit_count = count
        return qs

    def offset(self, count: int) -> 'QuerySet':
        qs = self._clone()
        qs._offset_count = count
        return qs

    def search(self, query: str, *fields) -> 'QuerySet':
        """
        Perform full-text search across specified fields.
        
        Args:
            query: Search term(s)
            *fields: Fields to search in. If none provided, searches all text fields.
            
        Returns:
            QuerySet filtered by search criteria
            
        Example:
            # Search across all text fields
            User.objects.search("john doe")
            
            # Search specific fields
            User.objects.search("john", "name", "email")
        """
        if not query or not query.strip():
            return self
            
        qs = self._clone()
        # Extract optional model-level Meta config for search
        def _extract_search_meta(model_cls):
            meta = getattr(model_cls, 'Meta', None)
            if not meta:
                return None
            out = {}
            for key in (
                'search_fields',         # sequence[str]
                'search_config',         # postgres text search config
                'search_weights',        # dict[field->A|B|C|D]
                'sqlite_fts',            # bool or {'table': str}
            ):
                if hasattr(meta, key):
                    out[key] = getattr(meta, key)
            return out or None

        search_info = {
            'query': query.strip(),
            'fields': list(fields) if fields else None,
            'meta': _extract_search_meta(self.model),
        }
        
        # Add search filter to be processed by the database provider
        qs._filters.append({'type': 'search', 'search_info': search_info})
        return qs

    def values(self, *fields) -> 'QuerySet':
        qs = self._clone()
        qs._select_fields = list(fields) if fields else ["*"]
        qs._values_mode = True
        qs._values_fields = list(fields)
        return qs

    def values_list(self, *fields, flat=False) -> 'QuerySet':
        if flat and len(fields) != 1:
            raise ValueError("values_list() with flat=True can only be used with a single field")

        qs = self.values(*fields)
        qs._values_flat = flat
        return qs

    def distinct(self, field: str = None) -> 'QuerySet':
        qs = self._clone()
        if field:
            qs._select_fields = [f"DISTINCT {field}"]
        else:
            qs._select_fields = ["DISTINCT *"]
        return qs

    def all(self):
        """Return the queryset itself (unevaluated), Django-style."""
        return self._clone()

    def using(self, alias: str) -> 'QuerySet':
        """Return a QuerySet that will use the specified database alias."""
        qs = self._clone()
        qs._db_alias = alias
        return qs

    async def _fetch_all(self) -> List[Union[T, Dict, Any]]:
        # Ensure provider/dialect is initialized before constructing SQL
        provider = await self._get_provider()
        sql, params = self._build_query()
        results = await provider.fetchall(sql, tuple(params))

        if not self._values_mode:
            return [self._deserialize_result(result) for result in results if result]

        processed_results = []
        for row in results:
            row_dict = dict(row)
            if not self._values_fields:
                processed_results.append(row_dict)
                continue

            if self._values_flat:
                processed_results.append(row_dict.get(self._values_fields[0]))
            else:
                if len(self._values_fields) == 1:
                    processed_results.append((row_dict.get(self._values_fields[0]),))
                else:
                    processed_results.append(tuple(row_dict.get(f) for f in self._values_fields))
        return processed_results

    async def first(self) -> Optional[Union[T, Dict, Any]]:
        qs = self.limit(1)
        results = await qs._fetch_all()
        return results[0] if results else None

    async def last(self) -> Optional[T]:
        if self._values_mode:
            raise TypeError("Cannot call last() after values() or values_list()")

        if not self._order_by:
            # Default to model primary key when available; else 'id'
            order_field = None
            if self.model is not None and hasattr(self.model, '_fields'):
                try:
                    pk_fields = [name for name, f in self.model._neutronapi_fields_.items() if getattr(f, 'primary_key', False)]
                    if len(pk_fields) == 1:
                        order_field = pk_fields[0]
                except Exception:
                    pass
            if not order_field:
                order_field = 'id'
            qs = self.order_by(f'-{order_field}').limit(1)
        else:
            reversed_order = []
            for order in self._order_by:
                if order.endswith(' DESC'):
                    reversed_order.append(order.replace(' DESC', ' ASC'))
                else:
                    reversed_order.append(order.replace(' ASC', ' DESC'))
            qs = self._clone()
            qs._order_by = reversed_order
            qs = qs.limit(1)

        results = await qs._fetch_all()
        return results[0] if results else None

    async def count(self) -> int:
        # Ensure provider/dialect is initialized BEFORE cloning
        provider = await self._get_provider()
        
        qs = self._clone()
        qs._select_fields = ["COUNT(*)"]
        qs._order_by = []
        qs._limit_count = None
        qs._offset_count = None

        sql, params = qs._build_query()
        result = await provider.fetchone(sql, tuple(params))

        if result:
            return list(result.values())[0] if isinstance(result, dict) else result[0]
        return 0

    async def exists(self) -> bool:
        qs = self._clone()
        qs._select_fields = ["1"]  # Just select a literal 1, no field assumptions
        qs._limit_count = 1
        result = await qs.first()
        return result is not None

    def __len__(self):
        if self._result_cache is None:
            raise TypeError("Cannot determine length of unevaluated queryset. Use 'await' first.")
        return len(self._result_cache)

    def __iter__(self):
        if self._result_cache is None:
            raise TypeError("Cannot iterate over an unevaluated queryset. Use 'await' first.")
        return iter(self._result_cache)

    def __await__(self):
        async def _populate_and_return_self():
            if self._result_cache is None:
                self._result_cache = await self._fetch_all()
            return self
        return _populate_and_return_self().__await__()

    async def __aiter__(self):
        if self._result_cache is None:
            self._result_cache = await self._fetch_all()
        results = self._result_cache
        for item in results:
            yield item

    async def get(self, *args, **kwargs) -> T:
        if self._values_mode:
            raise TypeError("Cannot call get() after values() or values_list()")

        qs = self.filter(*args, **kwargs)
        results = await qs.limit(2)._fetch_all()

        if not results:
            raise self._model_class.DoesNotExist(f"{self._model_class.__name__} matching query does not exist.")
        elif len(results) > 1:
            raise MultipleObjectsReturned(f"get() returned more than one {self._model_class.__name__} -- it returned {len(results)}!")

        return results[0]

    async def get_or_none(self, *args, **kwargs) -> Optional[T]:
        try:
            return await self.get(*args, **kwargs)
        except (self._model_class.DoesNotExist, MultipleObjectsReturned):
            return None

    async def vector_search(self, query_vector, top_k: int = 10, **pre_filters):
        from .vector_search import VectorSearch
        vs = VectorSearch(self)
        return await vs.vector_search(query_vector, top_k, **pre_filters)

    async def delete(self) -> int:
        # Ensure provider/dialect is initialized before constructing SQL
        provider = await self._get_provider()
        where_clause, params = self._build_where_clause()

        if not where_clause:
            # Allow deleting all objects if no filters are provided
            sql = f"DELETE FROM {self.table}"
            params = []
        else:
            sql = f"DELETE FROM {self.table} WHERE {where_clause}"

        await provider.execute(sql, tuple(params))
        return 1

    async def create(self, **kwargs):
        """Create a new record in the database by instantiating and saving the model."""
        # Convert any Enum values
        converted_kwargs = self._convert_enum_values(kwargs)
        instance = self._model_class(**converted_kwargs)
        await instance.save(create=True, using=self._db_alias)
        return instance


    async def update(self, **kwargs) -> int:
        if not kwargs:
            return 0

        kwargs['modified'] = datetime.datetime.now(tz=datetime.timezone.utc)

        # Ensure provider/dialect is initialized before constructing SQL
        provider = await self._get_provider()

        set_clauses = []
        set_params = []
        param_counter = 1

        for field, value in kwargs.items():
            if field in self._json_fields:
                value = self.provider.serialize(value)

            # *** FIX: Use correct placeholder based on dialect ***
            placeholder = '?' if self._is_sqlite else f'${param_counter}'
            set_clauses.append(f"{field} = {placeholder}")
            set_params.append(value)
            param_counter += 1

        where_clause, where_params = self._build_where_clause(param_counter)

        if not where_clause:
            raise ValueError("Cannot update all objects without filters.")

        sql = f"UPDATE {self.table} SET {', '.join(set_clauses)} WHERE {where_clause}"
        params = set_params + where_params

        await provider.execute(sql, tuple(params))
        return 1

    def _convert_enum_values(self, data: dict) -> dict:
        """Convert any Enum values in a dict to their string values."""
        converted = {}
        for key, value in data.items():
            if hasattr(value, 'value'):  # Enum-like object
                converted[key] = value.value
            else:
                converted[key] = value
        return converted

    def _clone(self) -> 'QuerySet':
        qs = QuerySet(self._model_class)
        qs._filters = self._filters.copy()
        qs._order_by = self._order_by.copy()
        qs._limit_count = self._limit_count
        qs._offset_count = self._offset_count
        qs._select_fields = self._select_fields.copy()
        qs._values_mode = self._values_mode
        qs._values_fields = self._values_fields.copy()
        qs._values_flat = self._values_flat
        # Copy provider-specific state
        qs.table = self.table
        qs.provider = self.provider
        qs._is_sqlite = self._is_sqlite
        qs._db_alias = self._db_alias
        return qs

    def _build_where_clause(self, param_start: int = 1) -> tuple:
        if not self._filters:
            return "", []

        all_conditions = []
        all_params = []
        param_counter = param_start

        for filter_item in self._filters:
            if filter_item.get('type') == 'q_object':
                q_condition, q_params = self._build_q_condition(filter_item['q_object'], param_counter)
                if q_condition:
                    all_conditions.append(f"({q_condition})")
                    all_params.extend(q_params)
                    param_counter += len(q_params)
            elif filter_item.get('type') == 'search':
                search_condition, search_params = self._build_search_condition(filter_item['search_info'], param_counter)
                if search_condition:
                    all_conditions.append(f"({search_condition})")
                    all_params.extend(search_params)
                    param_counter += len(search_params)

        if not all_conditions:
            return "", []

        return " AND ".join(all_conditions), all_params

    def _build_search_condition(self, search_info: dict, param_start: int) -> tuple:
        """Build search condition using database-specific full-text search.

        Note: Provider must already be initialized via _get_provider() before this is called.
        """
        provider = self.provider

        # Delegate to provider for database-specific search implementation
        if provider and hasattr(provider, 'build_search_condition'):
            # Pass table name so providers can target FTS tables when applicable
            return provider.build_search_condition(
                self.table,
                search_info,
                self.model._neutronapi_fields_,
                param_start,
                self._is_sqlite or False,
            )

        # Fallback to basic LIKE search if provider doesn't support full-text search
        return self._build_fallback_search_condition(search_info, param_start)
    
    def _build_fallback_search_condition(self, search_info: dict, param_start: int) -> tuple:
        """Fallback search using LIKE for databases without full-text search support."""
        query = search_info['query']
        search_fields = search_info.get('fields')
        
        # If no specific fields provided, search all text-like fields
        if not search_fields:
            search_fields = []
            for name, field in self.model._neutronapi_fields_.items():
                if hasattr(field, '__class__') and (
                    'CharField' in field.__class__.__name__ or 'TextField' in field.__class__.__name__
                ):
                    search_fields.append(name)
        
        if not search_fields:
            return "", []
        
        # Build LIKE conditions for each field
        conditions = []
        params = []
        param_counter = param_start
        
        for field in search_fields:
            if self._is_sqlite:
                conditions.append(f'"{field}" LIKE ?')
            else:
                conditions.append(f'"{field}" ILIKE ${param_counter}')
                param_counter += 1
            params.append(f'%{query}%')
        
        return " OR ".join(conditions), params

    def _build_q_condition(self, q_obj: 'Q', param_start: int) -> tuple:
        """Recursively build SQL condition and parameters from a Q object."""
        parts = []
        params = []
        param_counter = param_start

        # Handle case where all children are Q objects (combined Q objects)
        if all(isinstance(child, Q) for child in q_obj.children):
            for child_q in q_obj.children:
                condition, q_params = self._build_q_condition(child_q, param_counter)
                if condition:
                    parts.append(f"({condition})")
                    params.extend(q_params)
                    param_counter += len(q_params)
        else:
            # Handle mixed children (tuples and Q objects)
            for child in q_obj.children:
                if isinstance(child, Q):
                    condition, q_params = self._build_q_condition(child, param_counter)
                    if condition:
                        parts.append(f"({condition})")
                        params.extend(q_params)
                        param_counter += len(q_params)
                elif isinstance(child, tuple) and len(child) == 2:
                    field, value = child
                    lookup_parts = field.split('__')
                    field_name = lookup_parts[0]
                    
                    # Handle JSON fields differently - they may have nested paths
                    if field_name in self._json_fields:
                        # For JSON fields, check if the last part is a lookup type
                        potential_lookup = lookup_parts[-1] if len(lookup_parts) > 1 else 'exact'
                        valid_lookups = {'exact', 'iexact', 'contains', 'icontains', 'startswith', 'endswith', 'gt', 'gte', 'lt', 'lte', 'in', 'isnull'}
                        
                        if potential_lookup in valid_lookups:
                            lookup_type = potential_lookup
                            path = lookup_parts[1:-1]  # Everything between field_name and lookup_type
                        else:
                            lookup_type = 'exact'
                            path = lookup_parts[1:]    # Everything after field_name
                        
                        # Process as JSON field
                        json_filter = {'field': field_name, 'path': path, 'lookup': lookup_type, 'value': value}
                        condition, json_params = self._build_json_condition(json_filter, param_counter)
                        if condition:
                            parts.append(condition)
                            params.extend(json_params)
                            param_counter += len(json_params)
                        continue
                    else:
                        # Regular field
                        lookup_type = 'exact' if len(lookup_parts) == 1 else lookup_parts[-1]

                    # *** FIX: Simplified map, placeholder logic is now separate ***
                    lookup_map = {
                        'exact': '=', 'iexact': '=', 'contains': 'LIKE', 'icontains': 'ILIKE',
                        'startswith': 'LIKE', 'endswith': 'LIKE', 'gt': '>', 'gte': '>=',
                        'lt': '<', 'lte': '<=', 'in': 'IN', 'isnull': 'IS NULL'
                    }

                    # For SQLite, ILIKE is not standard, use LIKE with LOWER()
                    if self._is_sqlite and lookup_type == 'icontains':
                        lookup_type = 'contains'  # Will be handled by LOWER() later

                    if field_name in self._json_fields:
                        if lookup_type in lookup_map:
                            # Standard lookup like data__account__exact='value'
                            json_path = lookup_parts[1:-1]  # ['account']
                            actual_lookup = lookup_type
                        else:
                            # JSON key lookup like data__account='value' (defaults to exact)
                            json_path = lookup_parts[1:]  # ['account']
                            actual_lookup = 'exact'
                        json_filter = {'field': field_name, 'path': json_path, 'lookup': actual_lookup, 'value': value}

                        condition, json_params = self._build_json_condition(json_filter, param_counter)
                        if condition:
                            parts.append(condition)
                            params.extend(json_params)
                            param_counter += len(json_params)
                    elif lookup_type == 'search':
                        # Field-specific full-text search (__search)
                        search_info = {'query': value, 'fields': [field_name]}
                        condition, s_params = self._build_search_condition(search_info, param_counter)
                        if condition:
                            parts.append(condition)
                            params.extend(s_params)
                            param_counter += len(s_params)
                    else:
                        # Check if lookup_type is supported for non-JSON fields
                        if lookup_type not in lookup_map:
                            raise ValueError(f"Unsupported lookup type: {lookup_type}")

                        if lookup_type == 'in':
                            if not hasattr(value, '__iter__') or isinstance(value, str):
                                raise ValueError(f"Value for 'in' lookup must be an iterable. Got {type(value)}")
                            if not value:
                                parts.append("1=0")
                            else:
                                # Convert each item in the list using field's to_db method
                                converted_values = []
                                if hasattr(self.model, '_neutronapi_fields_') and field_name in self.model._neutronapi_fields_:
                                    field = self.model._neutronapi_fields_[field_name]
                                    for item in value:
                                        db_value = item
                                        if hasattr(field, 'to_db'):
                                            db_value = field.to_db(item)
                                            # Let the provider handle final conversion for query parameters
                                            if self.provider and hasattr(self.provider, 'convert_query_param'):
                                                db_value = self.provider.convert_query_param(db_value, field)
                                        converted_values.append(db_value)
                                else:
                                    converted_values = list(value)

                                # Generate correct placeholders for IN clause
                                if self._is_sqlite:
                                    placeholders = ', '.join(['?'] * len(converted_values))
                                else:
                                    placeholders = ', '.join(
                                        [f'${i}' for i in range(param_counter, param_counter + len(converted_values))])

                                condition = f"{field_name} IN ({placeholders})"
                                parts.append(condition)
                                params.extend(converted_values)
                                param_counter += len(converted_values)
                        elif lookup_type == 'isnull':
                            condition = f"{field_name} IS NULL" if value else f"{field_name} IS NOT NULL"
                            parts.append(condition)
                        else:
                            # *** FIX: Centralized placeholder and condition generation ***
                            op = lookup_map[lookup_type]
                            placeholder = '?' if self._is_sqlite else f'${param_counter}'

                            # Handle case-insensitivity for SQLite
                            field_expr = field_name
                            value_expr = placeholder
                            if self._is_sqlite and lookup_type in ['iexact', 'icontains']:
                                field_expr = f"LOWER({field_name})"
                                value_expr = f"LOWER({placeholder})"

                            condition = f"{field_expr} {op} {value_expr}"
                            parts.append(condition)

                            # For string-based lookups, don't apply provider conversion to datetime fields
                            if lookup_type in ['contains', 'icontains']:
                                # String lookups - check if this makes sense for the field type
                                if hasattr(self.model, '_neutronapi_fields_') and field_name in self.model._neutronapi_fields_:
                                    field = self.model._neutronapi_fields_[field_name]
                                    if hasattr(field, '__class__') and 'DateTimeField' in field.__class__.__name__:
                                        raise ValueError(f"Lookup '{lookup_type}' is not supported for DateTimeField")
                                params.append(f"%{value}%")
                            elif lookup_type == 'startswith':
                                if hasattr(self.model, '_neutronapi_fields_') and field_name in self.model._neutronapi_fields_:
                                    field = self.model._neutronapi_fields_[field_name]
                                    if hasattr(field, '__class__') and 'DateTimeField' in field.__class__.__name__:
                                        raise ValueError(f"Lookup 'startswith' is not supported for DateTimeField")
                                params.append(f"{value}%")
                            elif lookup_type == 'endswith':
                                if hasattr(self.model, '_neutronapi_fields_') and field_name in self.model._neutronapi_fields_:
                                    field = self.model._neutronapi_fields_[field_name]
                                    if hasattr(field, '__class__') and 'DateTimeField' in field.__class__.__name__:
                                        raise ValueError(f"Lookup 'endswith' is not supported for DateTimeField")
                                params.append(f"%{value}")
                            else:
                                # Comparison lookups - apply provider conversion for datetime fields
                                db_value = value
                                if hasattr(self.model, '_neutronapi_fields_') and field_name in self.model._neutronapi_fields_:
                                    field = self.model._neutronapi_fields_[field_name]
                                    if hasattr(field, 'to_db'):
                                        db_value = field.to_db(value)
                                        # Let the provider handle final conversion for query parameters
                                        if self.provider and hasattr(self.provider, 'convert_query_param'):
                                            db_value = self.provider.convert_query_param(db_value, field)
                                params.append(db_value)
                            param_counter += 1
                else:
                    raise ValueError(f"Invalid Q object child: {child}")

        condition_str = f" {q_obj.connector} ".join(parts)
        if q_obj.negated and condition_str:
            condition_str = f"NOT ({condition_str})"

        return condition_str, params

    def _build_json_condition(self, filter_item: dict, param_start: int) -> tuple:
        field, path, lookup, value = filter_item['field'], filter_item['path'], filter_item['lookup'], filter_item[
            'value']

        params = []
        condition = ""
        # *** FIX: Use correct placeholder based on dialect ***
        placeholder = '?' if self._is_sqlite else f'${param_start}'

        if self._is_sqlite:
            json_path_str = f"$.{'.'.join(path)}" if path else "$"
            json_expr = f"json_extract({field}, '{json_path_str}')"

            if lookup == 'isnull':
                condition = f"{json_expr} IS {'NULL' if value else 'NOT NULL'}"
            else:
                op_map = {'gt': '>', 'gte': '>=', 'lt': '<', 'lte': '<='}
                if lookup == 'exact':
                    # Special handling for boolean values
                    if isinstance(value, bool):
                        bool_text = json.dumps(value)
                        base_condition = f"(CAST({json_expr} AS TEXT) = {placeholder} OR CAST({json_expr} AS TEXT) = ?)"
                        params.extend([bool_text, "1" if value else "0"])
                        # Make condition NULL-aware to work correctly with exclude (NOT)
                        condition = f"({json_expr} IS NOT NULL AND {base_condition})"
                    elif isinstance(value, (dict, list)):
                        serialized = json.dumps(value, separators=(',', ':'))
                        base_condition = f"CAST({json_expr} AS TEXT) = {placeholder}"
                        params.append(serialized)
                        # Make condition NULL-aware to work correctly with exclude (NOT)
                        condition = f"({json_expr} IS NOT NULL AND {base_condition})"
                    else:
                        base_condition = f"CAST({json_expr} AS TEXT) = {placeholder}"
                        params.append(str(value))
                        # Make condition NULL-aware to work correctly with exclude (NOT)
                        condition = f"({json_expr} IS NOT NULL AND {base_condition})"
                elif lookup == 'contains':
                    base_condition = f"CAST({json_expr} AS TEXT) LIKE {placeholder}"
                    params.append(f"%{value}%")
                    # Make condition NULL-aware to work correctly with exclude (NOT)
                    condition = f"({json_expr} IS NOT NULL AND {base_condition})"
                elif lookup == 'icontains':
                    base_condition = f"LOWER(CAST({json_expr} AS TEXT)) LIKE LOWER({placeholder})"
                    params.append(f"%{value}%")
                    # Make condition NULL-aware to work correctly with exclude (NOT)
                    condition = f"({json_expr} IS NOT NULL AND {base_condition})"
                elif lookup in op_map:
                    op = op_map[lookup]
                    cast_type = "NUMERIC" if isinstance(value, (int, float)) else "TEXT"
                    base_condition = f"CAST({json_expr} AS {cast_type}) {op} {placeholder}"
                    params.append(str(value) if isinstance(value, (int, float)) else value)
                    # Make condition NULL-aware to work correctly with exclude (NOT)
                    condition = f"({json_expr} IS NOT NULL AND {base_condition})"
        else:  # PostgreSQL JSONB
            if path:
                # Build proper PostgreSQL JSON path with literal keys
                if len(path) == 1:
                    # Single key: field->>'key'
                    text_path_expr = f"{field}->>'{path[0]}'"
                    path_expr = f"{field}->'{path[0]}'"
                else:
                    # Multiple keys: field->'key1'->'key2'->>'finalkey'
                    json_path_parts = "->".join(f"'{key}'" for key in path[:-1])
                    text_path_expr = f"{field}->{json_path_parts}->>'{path[-1]}'"
                    path_expr = f"{field}->{json_path_parts}->'{path[-1]}'"
            else:
                # No path, just the field itself
                path_expr = field
                text_path_expr = f"{field}::text"

            if lookup == 'isnull':
                condition = f"{path_expr} IS {'NULL' if value else 'NOT NULL'}"
            elif lookup == 'exact':
                if not path:
                    # Compare whole JSON object via jsonb for structural equality
                    if isinstance(value, (dict, list)):
                        serialized = json.dumps(value, separators=(',', ':'))
                    elif isinstance(value, bool):
                        serialized = json.dumps(value)
                    else:
                        serialized = str(value)
                    # Make condition NULL-aware to work correctly with exclude (NOT)
                    base_condition = f"{path_expr} = {placeholder}::jsonb"
                    condition = f"({path_expr} IS NOT NULL AND {base_condition})"
                    params.append(serialized)
                else:
                    if isinstance(value, bool):
                        serialized = json.dumps(value)
                    elif isinstance(value, (dict, list)):
                        serialized = json.dumps(value, separators=(',', ':'))
                    else:
                        serialized = str(value)
                    # Make condition NULL-aware to work correctly with exclude (NOT)
                    base_condition = f"{text_path_expr} = {placeholder}::text"
                    condition = f"({path_expr} IS NOT NULL AND {base_condition})"
                    params.append(serialized)
            elif lookup == 'contains':
                condition = f"{text_path_expr} LIKE {placeholder}::text"
                params.append(f"%{value}%")
            elif lookup == 'startswith':
                condition = f"{text_path_expr} LIKE {placeholder}::text"
                params.append(f"{value}%")
            elif lookup == 'endswith':
                condition = f"{text_path_expr} LIKE {placeholder}::text"
                params.append(f"%{value}")
            elif lookup == 'icontains':
                condition = f"LOWER({text_path_expr}) LIKE LOWER({placeholder}::text)"
                params.append(f"%{value}%")
            else:
                # Handle comparison operators (gt, gte, lt, lte)
                op_map = {'gt': '>', 'gte': '>=', 'lt': '<', 'lte': '<='}
                if lookup in op_map:
                    op = op_map[lookup]
                    # Cast JSON text to appropriate type for comparison
                    if isinstance(value, (int, float)):
                        condition = f"CAST({text_path_expr} AS NUMERIC) {op} {placeholder}"
                        params.append(str(value))  # Convert to string for PostgreSQL
                    else:
                        condition = f"{text_path_expr} {op} {placeholder}::text"
                        params.append(str(value))

        return condition, params

    def _build_query(self) -> tuple:
        select_clause = ", ".join(self._select_fields)
        sql = f"SELECT {select_clause} FROM {self.table}"

        where_clause, params = self._build_where_clause()
        if where_clause:
            sql += f" WHERE {where_clause}"

        # Optional: add rank-based ordering when requested and a search filter exists
        rank_order_clause = None
        rank_params = []
        if self._search_order_by_rank and any(f.get('type') == 'search' for f in self._filters):
            # Use the first search filter as the basis for ranking
            search_info = next(f['search_info'] for f in self._filters if f.get('type') == 'search')
            if self.provider and hasattr(self.provider, 'build_search_order_by'):
                # Start indexing after existing params
                next_index = (len(params) + 1) if not self._is_sqlite else 1
                try:
                    rank_order_clause, rank_params = self.provider.build_search_order_by(
                        self.table,
                        search_info,
                        self.model._neutronapi_fields_,
                        next_index,
                        self._is_sqlite or False,
                    )
                except Exception:
                    rank_order_clause, rank_params = None, []

        order_clauses = []
        if rank_order_clause:
            order_clauses.append(rank_order_clause)
        if self._order_by:
            order_parts = []
            for order_item in self._order_by:
                if isinstance(order_item, dict):
                    # New format with field/direction
                    field_name = order_item['field']
                    direction = order_item['direction']

                    # Check if this is a JSON field lookup
                    if '__' in field_name and self._is_json_field_lookup(field_name):
                        json_expr = self._build_json_order_expression(field_name)
                        order_parts.append(f"{json_expr} {direction}")
                    else:
                        # Regular field ordering
                        order_parts.append(f"{field_name} {direction}")
                else:
                    # Legacy string format (for backward compatibility)
                    order_parts.append(order_item)

            if order_parts:
                order_clauses.append(', '.join(order_parts))
        if order_clauses:
            sql += f" ORDER BY {', '.join(order_clauses)}"
            params.extend(rank_params)

        if self._limit_count is not None:
            sql += f" LIMIT {self._limit_count}"

        if self._offset_count is not None:
            sql += f" OFFSET {self._offset_count}"

        return sql, params

    def _deserialize_result(self, result: Dict[str, Any]) -> Optional[T]:
        if not result:
            return None

        result_dict = dict(result)

        if hasattr(self.provider, 'deserialize'):
            for field in self._json_fields:
                if result_dict.get(field) and isinstance(result_dict[field], str):
                    result_dict[field] = self.provider.deserialize(result_dict[field])

        # Keep datetime objects as datetime objects, don't convert to strings

        for field in self._json_fields:
            result_dict.setdefault(field, {})

        # Return a Model instance instead of opinionated Object
        instance = self.model(**result_dict)
        instance.pk = instance.id  # Set pk to indicate this came from database
        return instance




class MultipleObjectsReturned(Exception):
    pass
