# core/api.py
import json
import re
from dataclasses import dataclass
from functools import wraps
from typing import (
    Any,
    List,
    Tuple,
    Callable,
    Optional,
    Dict,
    TypeVar,
    Union,
    Type,
    TYPE_CHECKING,
)

if TYPE_CHECKING:
    pass
from urllib.parse import parse_qs

from neutronapi.api import exceptions
from neutronapi.encoders import CustomJSONEncoder
from neutronapi.db.models import Model

T = TypeVar("T", bound="Model")

# Import the global background scheduler instance
# Background tasks are managed by the application; no global instance here

# Type aliases for schemas
JsonSchemaDict = Dict[str, Any]
RequestHandler = Callable[..., Any]
Scope = Dict[str, Any]
Receive = Callable[[], Any]
Send = Callable[[Dict[str, Any]], None]


class Response:
    """HTTP Response handler for API responses.
    
    Handles JSON serialization, status codes, and headers for HTTP responses.
    """

    def __init__(
        self,
        body: Any = None,
        status_code: int = 200,
        headers: Optional[List[Tuple[bytes, bytes]]] = None,
        media_type: str = "application/json",
        indent: int = 2,
    ) -> None:
        """Initialize HTTP response.
        
        Args:
            body: Response body data (will be JSON-serialized)
            status_code: HTTP status code (default: 200)
            headers: List of header tuples as (name, value) bytes
            media_type: Content-Type header value
            indent: JSON indentation for pretty printing
        """
        self.body = body
        self.status_code = status_code
        self.headers = headers or []
        self.media_type = media_type
        self.indent = indent

        if not any(name.lower() == b"content-type" for name, _ in self.headers):
            self.headers.append((b"content-type", self.media_type.encode()))

    def __repr__(self):
        return (
            f"Response(body={self.body}, status_code={self.status_code}, "
            f"headers={self.headers}, media_type={self.media_type}, indent={self.indent})"
        )

    def __str__(self):
        return (
            f"Response(body={self.body}, status_code={self.status_code}, "
            f"headers={self.headers}, media_type={self.media_type}, indent={self.indent})"
        )

    async def __call__(self, scope, receive, send):
        """Send the response."""
        await send(
            {
                "type": "http.response.start",
                "status": self.status_code,
                "headers": self.headers,
            }
        )

        body_bytes = b""
        if self.body is not None:
            if self.media_type == "application/json" and isinstance(
                self.body, (dict, list)
            ):
                body_bytes = json.dumps(
                    self.body,
                    cls=CustomJSONEncoder,
                    indent=self.indent,
                    sort_keys=True,
                    ensure_ascii=False,
                    separators=(",", ": "),
                ).encode("utf-8")
            elif isinstance(self.body, str):
                body_bytes = self.body.encode("utf-8")
            elif isinstance(self.body, bytes):
                body_bytes = self.body

        await send(
            {"type": "http.response.body", "body": body_bytes, "more_body": False}
        )


@dataclass
class Endpoint:
    """Metadata for API endpoints."""

    path: str
    methods: List[str]
    handler: Callable
    name: Optional[str] = None
    description: Optional[str] = None
    authentication_class: Optional[Any] = None
    permission_classes: Optional[List[Any]] = None
    throttle_classes: Optional[List[Any]] = None
    # OpenAPI-specific metadata
    summary: Optional[str] = None
    tags: Optional[List[str]] = None
    request_schema: Optional[JsonSchemaDict] = None
    response_schema: Optional[JsonSchemaDict] = None
    responses: Optional[Dict[int, JsonSchemaDict]] = None
    parameters: Optional[List[Dict[str, Any]]] = None
    deprecated: bool = False
    # Documentation control
    include_in_docs: bool = True
    # Body parsing control
    skip_body_parsing: bool = False
    # Pagination control for OpenAPI
    paginated: bool = True


class API:
    """Base API class with optional CRUD functionality when model is specified.

    Request/Response Schema Architecture:
    - request_schema: JSON schema for request body validation (used for POST, PUT, PATCH)
    - response_schema: JSON schema for single item responses (GET /{id}, POST, PUT, PATCH)
    - list_response_schema: JSON schema for list responses (GET /) - if not set, uses paginated response with
      response_schema items
    - error_responses: Dict mapping HTTP status codes to error response schemas

    Schema Usage by HTTP Method:
    - GET (list): Uses list_response_schema or auto-generated pagination schema with response_schema items
    - GET (detail): Uses response_schema
    - POST: Request uses request_schema, response uses response_schema
    - PUT/PATCH: Request uses request_schema, response uses response_schema
    - DELETE: No request/response schema (returns 204 No Content)
    
    Dependency Injection:
    The Application automatically injects the 'registry' attribute:
    - self.registry: Dict[str, Any] - Universal registry for all injected components
    
    Example:
        >>> class UserAPI(API):
        ...     resource = "/users"
        ...
        ...     @API.endpoint("/", methods=["GET"])
        ...     async def list_users(self, scope, receive, send):
        ...         logger = self.registry.get('utils:logger')
        ...         db = self.registry.get('services:database')
        ...         # ... your logic here
    """

    model: Optional[Type[Model]] = None
    resource: str = ""
    authentication_class: Optional[Any] = None
    name: Optional[str] = None
    page_size: int = 10
    lookup_field: str = "id"

    # OpenAPI documentation fields
    title: Optional[str] = None
    description: Optional[str] = None
    tags: Optional[List[str]] = None

    # Request/Response schema fields - see class docstring for usage
    request_schema: Optional[JsonSchemaDict] = None
    response_schema: Optional[JsonSchemaDict] = None
    list_response_schema: Optional[JsonSchemaDict] = None
    error_responses: Optional[Dict[int, JsonSchemaDict]] = None

    # Documentation control
    hidden: bool = False  # If True, exclude from OpenAPI docs by default (e.g., internal/debug APIs)
    
    # Dependency injection attributes (set by Application)
    registry: Dict[str, Any]  # Universal registry for all components

    def __init__(
        self,
        resource: str = "",
        routes: Optional[List[Tuple]] = None,
        permission_classes: Optional[List[Any]] = None,
        throttle_classes: Optional[List[Any]] = None,
        authentication_class: Optional[Any] = None,
        name: Optional[str] = None,
        page_size: Optional[int] = None,
        lookup_field: Optional[str] = None,
        title: Optional[str] = None,
        description: Optional[str] = None,
        tags: Optional[List[str]] = None,
    ):
        self.resource = self.resource or resource.rstrip("/")
        self.routes = []
        self.kwargs = {}
        self.permission_classes = permission_classes or []
        self.throttle_classes = throttle_classes or []
        self.authentication_class = authentication_class or self.authentication_class

        # Allow overriding configuration via init
        if self.page_size is None:
            self.page_size = page_size
        if lookup_field is not None:
            self.lookup_field = lookup_field

        # OpenAPI documentation overrides
        if title is not None:
            self.title = title
        if description is not None:
            self.description = description
        if tags is not None:
            self.tags = tags

        # Setup routes
        if routes:
            for route in routes:
                if len(route) == 2:
                    path, handler = route
                    self.add_route(path=path, handler=handler)
                else:
                    path, handler, methods = route
                    self.add_route(path=path, handler=handler, methods=methods)

        self._register_endpoints()

    def params(self, scope: Scope) -> Dict[str, Union[str, List[str]]]:
        """Get the current request parameters.
        
        Args:
            scope: ASGI scope dict
            
        Returns:
            Dict of query parameters with string values or lists of strings
        """
        query_string = scope.get("query_string", b"").decode()
        params: Dict[str, Union[str, List[str]]] = {}

        if query_string:
            parsed = parse_qs(query_string)
            # Convert all single-item lists to single values
            params = {k: v[0] if len(v) == 1 else v for k, v in parsed.items()}
        return params

    async def _process_client_params(self, scope: Scope) -> Scope:
        """Process client-side parameters for pagination and ordering.
        
        Args:
            scope: ASGI scope dict
            
        Returns:
            Modified scope with pagination and ordering parameters
        """
        params = self.params(scope)
        scope["params"] = params

        # Handle pagination
        scope["page"] = int(params.get("page", "1"))
        scope["page_size"] = int(
            params.get("page_size", str(getattr(self, "page_size", 10)))
        )

        # Handle ordering
        if "ordering" in params:
            scope["ordering"] = params["ordering"]
        if "order_direction" in params:
            scope["order_direction"] = params["order_direction"]

        return scope

    @staticmethod
    def endpoint(
        path: str,
        methods: Optional[Union[str, List[str]]] = None,
        authentication_class: Optional[Any] = None,
        permission_classes: Optional[List[Any]] = None,
        throttle_classes: Optional[List[Any]] = None,
        name: Optional[str] = None,
        # Endpoint middlewares (instances only)
        middlewares: Optional[List[Any]] = None,
        # Parsers list (instances only); default is JSONParser when None
        parsers: Optional[List[Any]] = None,
        # Endpoint middleware & parsers (instances only)
        # OpenAPI documentation parameters
        summary: Optional[str] = None,
        description: Optional[str] = None,
        tags: Optional[List[str]] = None,
        request_schema: Optional[JsonSchemaDict] = None,
        response_schema: Optional[JsonSchemaDict] = None,
        responses: Optional[Dict[int, JsonSchemaDict]] = None,
        parameters: Optional[List[Dict[str, Any]]] = None,
        deprecated: bool = False,
        # Documentation control
        include_in_docs: bool = True,
        # Body parsing control
        skip_body_parsing: bool = False,
        # Pagination control for OpenAPI
        paginated: bool = True,
    ) -> Callable:
        """Decorator for defining API endpoints.

        Args:
            path: URL path pattern (e.g., "/users/<int:id>")
            methods: HTTP methods (default: ["GET"])
            middlewares: List of endpoint-level middleware instances (wraps handler; inside global
                middlewares)
            parsers: List of parser instances (JSONParser, FormParser, MultiPartParser, BinaryParser).
                If not provided, defaults to JSON.
            permission_classes: List of permission classes
            throttle_classes: List of throttle classes
            name: Endpoint name for URL reversing

            # OpenAPI Documentation Parameters:
            summary: Brief summary of the endpoint
            description: Detailed description (defaults to function docstring)
            tags: List of tags for grouping operations
            request_schema: JSON schema for request body validation
            response_schema: JSON schema for successful response
            responses: Dict mapping status codes to response schemas
            parameters: List of parameter definitions for query/path params
            deprecated: Whether this endpoint is deprecated
            include_in_docs: Whether to include this endpoint in generated documentation (default: True)

        Examples:
            @API.endpoint(
                "/users/<int:user_id>/profile",
                methods=["GET", "PUT"],
                summary="Get or update user profile",
                tags=["Users", "Profiles"],
                include_in_docs=True,  # Explicitly include in docs
                request_schema={
                    "type": "object",
                    "properties": {
                        "name": {"type": "string"},
                        "email": {"type": "string", "format": "email"}
                    }
                },
                response_schema={
                    "type": "object",
                    "properties": {
                        "id": {"type": "integer"},
                        "name": {"type": "string"},
                        "email": {"type": "string"}
                    }
                },
                responses={
                    400: {"type": "object", "properties": {"error": {"type": "string"}}},
                    404: {"type": "object", "properties": {"error": {"type": "string"}}}
                }
            )
            async def get_user_profile(self, scope, receive, send, user_id=None):
                # Implementation here
                pass

            # JSON (default parser)
            @API.endpoint("/users", methods=["POST"])
            async def create_user(self, scope, receive, send, **kwargs):
                data = kwargs["body"]  # dict parsed from JSON
                return await self.response({"ok": True})

            # Form
            from neutronapi.parsers import FormParser
            @API.endpoint("/login", methods=["POST"], parsers=[FormParser()])
            async def login(self, scope, receive, send, **kwargs):
                creds = kwargs["body"]  # dict from form urlencoded
                return await self.response({"ok": True})

            # Multipart
            from neutronapi.parsers import MultiPartParser
            @API.endpoint("/upload", methods=["POST"], parsers=[MultiPartParser()])
            async def upload(self, scope, receive, send, **kwargs):
                fields = kwargs["body"]
                file_bytes = kwargs.get("file")
                filename = kwargs.get("filename")
                return await self.response({"ok": True})

            # Binary
            from neutronapi.parsers import BinaryParser
            @API.endpoint("/blob", methods=["POST"], parsers=[BinaryParser()])
            async def put_blob(self, scope, receive, send, **kwargs):
                blob = kwargs["body"]  # bytes
                return await self.response(b"stored", media_type="text/plain")

            # Example of hiding an endpoint from documentation:
            @API.endpoint(
                "/internal/debug",
                methods=["GET"],
                include_in_docs=False,  # Hide from generated docs
                summary="Internal debug endpoint"
            )
            async def debug_endpoint(self, scope, receive, send):
                # This endpoint won't appear in OpenAPI/Swagger docs
                pass
        """
        if methods is None:
            methods = ["GET"]
        elif isinstance(methods, str):
            methods = [methods]

        methods = [m.upper() for m in methods]

        def decorator(func: Callable):
            @wraps(func)
            async def wrapper(self, *args, **kwargs):
                return await func(self, *args, **kwargs)

            endpoint_name = name if name else func.__name__

            wrapper._endpoint = Endpoint(
                path=path,
                methods=methods,
                handler=func,
                name=endpoint_name,
                description=description or func.__doc__,
                authentication_class=authentication_class,
                permission_classes=permission_classes,
                throttle_classes=throttle_classes,
                # OpenAPI metadata
                summary=summary,
                tags=tags,
                request_schema=request_schema,
                response_schema=response_schema,
                responses=responses,
                parameters=parameters,
                deprecated=deprecated,
                include_in_docs=include_in_docs,
                # Body parsing control
                skip_body_parsing=skip_body_parsing,
                # Pagination control
                paginated=paginated,
            )
            # Attach extra endpoint metadata for middlewares/parsers
            wrapper._endpoint_middlewares = middlewares or []
            wrapper._endpoint_parsers = parsers or []
            return wrapper

        return decorator

    @staticmethod
    def websocket(path: str) -> Callable:
        """Decorator for defining WebSocket endpoints."""

        def decorator(func: Callable):
            @wraps(func)
            async def wrapper(self, websocket, *args, **kwargs):
                return await func(self, websocket, *args, **kwargs)

            wrapper._websocket_metadata = {
                "path": path,
                "handler": func,
            }
            return wrapper

        return decorator

    async def ws_send(self, send: Send, payload: Dict[str, Any]) -> None:
        """Send data over WebSocket."""
        await send(
            {
                "type": "websocket.send",
                "text": json.dumps(payload, cls=CustomJSONEncoder),
            }
        )

    async def ws_receive(self, message: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        """Receive data from WebSocket."""
        if message["type"] != "websocket.receive":
            return None

        try:
            return json.loads(message["text"])
        except json.JSONDecodeError:
            return None

    async def ws_error(self, send: Send, message: str) -> None:
        """Send error over WebSocket."""
        await self.ws_send(send=send, payload={"type": "error", "message": message})

    async def ws_close(self, send: Send) -> None:
        """Close WebSocket connection."""
        await send({"type": "websocket.close", "code": 1000})

    async def get_base_queryset(self, scope: Scope) -> Any:
        """Get base queryset without filtering.
        
        Args:
            scope: ASGI scope dict
            
        Returns:
            Base model queryset
            
        Raises:
            NotImplementedError: If no model is defined
        """
        if not self.model:
            raise NotImplementedError("Model must be defined")
        return self.model.objects.using(self.model.db_alias)

    async def get_queryset(self, scope: Scope) -> Any:
        """Get queryset with ordering applied.
        
        Args:
            scope: ASGI scope dict with ordering parameters
            
        Returns:
            Queryset with ordering applied
        """
        queryset = await self.get_base_queryset(scope)

        # Handle ordering
        if ordering := scope.get("ordering"):
            order_direction = scope.get("order_direction", "ASC")
            if ordering.startswith("-"):
                ordering = ordering[1:]
                order_direction = "DESC"

            queryset = queryset.order_by(
                f"{'-' if order_direction == 'DESC' else ''}{ordering}"
            )

        return queryset

    async def get_instance(self, scope: Scope, **kwargs) -> T:
        """Get a single instance based on scope and kwargs."""
        queryset = await self.get_queryset(scope)
        try:
            lookup_value = kwargs[self.lookup_field]
            return await queryset.get(**{self.lookup_field: lookup_value})
        except self.model.DoesNotExist as e:
            raise exceptions.NotFound(
                f"{self.model.__name__} with {self.lookup_field} not found"
            ) from e

    async def transform(self, data):
        """Transform the data before saving."""
        if data.get("id"):
            data.pop("id")
        return data

    def _register_endpoints(self):
        """Registers endpoints decorated with @endpoint and @websocket."""
        # Collect all endpoint metadata first
        endpoints = []
        websockets = []

        for attr_name in dir(self):
            if attr_name == "CORTEX":
                continue
            attr = getattr(self, attr_name)
            if hasattr(attr, "_endpoint"):
                metadata = attr._endpoint
                endpoints.append((attr, metadata))
            elif hasattr(attr, "_websocket_metadata"):
                metadata = attr._websocket_metadata
                websockets.append((attr, metadata))

        # Sort endpoints by path specificity (static paths before dynamic patterns)
        def path_specificity(endpoint_data):
            attr, metadata = endpoint_data
            path = metadata.path
            # Count dynamic segments (lower is more specific)
            dynamic_segments = path.count("<")
            # Prioritize static paths over dynamic ones
            if dynamic_segments == 0:
                return (0, len(path.split("/")))  # Static paths first, then by depth
            else:
                return (
                    dynamic_segments,
                    len(path.split("/")),
                )  # Dynamic paths by number of params, then depth

        # Sort endpoints by specificity
        endpoints.sort(key=path_specificity)

        # Register endpoints in order of specificity
        for attr, metadata in endpoints:
            self.add_route(
                path=metadata.path,
                handler=attr,
                methods=metadata.methods,
                authentication_class=metadata.authentication_class,
                permission_classes=metadata.permission_classes,
                throttle_classes=metadata.throttle_classes,
                name=metadata.name,
                skip_body_parsing=metadata.skip_body_parsing,
            )

        # Register websockets
        for attr, metadata in websockets:
            path = metadata["path"]
            if not path.startswith(self.resource):
                path = self.resource + path
            if not path.startswith("/") and path:
                path = "/" + path
            pattern = self._convert_path_to_regex(path)
            self.routes.append(
                (
                    re.compile(pattern),
                    attr,
                    ["WEBSOCKET"],  # Special method type for websockets
                    [],  # No permission classes for now, can be added later
                    [],  # No throttle classes for now, can be added later
                    None,
                    path,
                    False,  # skip_body_parsing not applicable for websockets
                )
            )

    def add_route(
        self,
        path: str,
        handler: RequestHandler,
        methods: Optional[List[str]] = None,
        authentication_class: Optional[Any] = None,
        permission_classes: Optional[List[Any]] = None,
        throttle_classes: Optional[List[Any]] = None,
        name: Optional[str] = None,
        skip_body_parsing: bool = False,
    ) -> None:
        """Adds a new route to the API."""
        if methods is None:
            methods = ["GET"]

        if not path.startswith(self.resource):
            path = self.resource + path

        if not path.startswith("/") and path:
            path = "/" + path

        pattern = self._convert_path_to_regex(path)
        self.routes.append(
            (
                re.compile(pattern),
                handler,
                methods,
                permission_classes or self.permission_classes,
                throttle_classes or self.throttle_classes,
                name,
                path,
                skip_body_parsing,
            )
        )

    @staticmethod
    def _convert_path_to_regex(path):
        """Convert path pattern to regex."""
        if not path or path == "/":
            return "^/?$"

        path = path.strip("/")
        parts = path.split("/")
        pattern = []

        for part in parts:
            if part.startswith("<") and part.endswith(">"):
                param_match = re.match(r"<(\w+):(\w+)>", part)
                if param_match:
                    param_type, param_name = param_match.groups()
                    if param_type == "int":
                        pattern.append(f"(?P<{param_name}>[0-9]+)")
                    elif param_type == "str":
                        pattern.append(f"(?P<{param_name}>[^/]+)")
                    elif param_type == "path":
                        pattern.append(f"(?P<{param_name}>.+)")
                    elif param_type == "slug":
                        pattern.append(f"(?P<{param_name}>[-a-zA-Z0-9_]+)")
            else:
                pattern.append(re.escape(part))

        return f"^/{'/'.join(pattern)}?$"

    @staticmethod
    async def check_permissions(scope, permission_classes):
        """Checks if the request has the required permissions."""
        user = scope.get("user")
        for permission_class in permission_classes:
            if not await permission_class().has_permission(scope, user):
                raise exceptions.PermissionDenied()

    @staticmethod
    async def check_throttles(scope, throttle_classes):
        """Checks if the request should be throttled."""
        for throttle_class in throttle_classes:
            throttle = throttle_class()
            if not await throttle.allow_request(scope, "some_rate"):
                wait_time = await throttle.wait()
                raise exceptions.Throttled(wait=wait_time)

    async def handle(
        self, scope: Scope, receive: Receive, send: Send, **kwargs
    ) -> None:
        """Enhanced handle method with form data support and raw body storage."""
        # Dispatch websocket connections to separate handler
        if scope["type"] == "websocket":
            return await self.handle_websocket(scope, receive, send, **kwargs)

        try:
            scope = await self._process_client_params(scope)
            method = scope["method"]
            path = scope["path"].rstrip("/")

            (
                handler,
                kwargs,
                permission_classes,
                throttle_classes,
                _,
                _,
                skip_body_parsing,
            ) = await self.match(path, method)

            if handler is None:
                raise exceptions.MethodNotAllowed(method, path)

            if self.authentication_class:
                await self.authentication_class.authorize(scope)

            await self.check_permissions(scope, permission_classes)
            await self.check_throttles(scope, throttle_classes)

            # Pass scope params through kwargs
            kwargs.update(
                {
                    "page": scope.get("page", 1),
                    "page_size": scope.get("page_size", self.page_size),
                    "ordering": scope.get("ordering"),
                    # Add params from scope to kwargs so handlers can access them
                    "params": scope.get("params", {}),
                }
            )

            # Parse request body using parser instances
            from neutronapi.parsers import JSONParser
            headers_dict = dict(scope.get("headers", []))
            raw_body = b""
            if method in ["POST", "PUT", "PATCH"]:
                # Read complete body once
                msg = await receive()
                if msg.get("type") == "http.request":
                    raw_body = msg.get("body", b"")
                    more = msg.get("more_body", False)
                    while more:
                        msg = await receive()
                        raw_body += msg.get("body", b"")
                        more = msg.get("more_body", False)

            # Endpoint-specific parsers
            endpoint_parsers = []
            if hasattr(handler, '_endpoint_parsers'):
                endpoint_parsers = list(getattr(handler, '_endpoint_parsers') or [])

            parsed_kwargs = {}
            if method in ["POST", "PUT", "PATCH"]:
                # Select parser: first matching endpoint parser; else default JSONParser()
                parser = None
                for p in endpoint_parsers:
                    try:
                        if hasattr(p, 'matches') and p.matches(headers_dict):
                            parser = p
                            break
                    except Exception:
                        continue
                if parser is None:
                    parser = JSONParser()
                parsed_kwargs = await parser.parse(scope, receive, raw_body=raw_body, headers=headers_dict)
                kwargs.update(parsed_kwargs)

            # Compose endpoint-level middlewares (instances only)
            endpoint_mws = []
            if hasattr(handler, '_endpoint_middlewares'):
                endpoint_mws = list(getattr(handler, '_endpoint_middlewares') or [])

            async def handler_app(scope2, receive2, send2):
                response = await handler(scope2, receive2, send2, **kwargs)
                if response is None:
                    return
                elif isinstance(response, Response):
                    return await response(scope2, receive2, send2)
                else:
                    raise ValueError(f"Invalid response type: {type(response)}")

            app_to_call = handler_app
            for mw in reversed(endpoint_mws):
                if hasattr(mw, 'app'):
                    mw.app = app_to_call
                if hasattr(mw, 'router'):
                    mw.router = app_to_call
                app_to_call = mw

            # Call the composed endpoint app
            await app_to_call(scope, receive, send)

        except exceptions.APIException as e:
            # Unified API error shape
            response = Response(
                body=e.to_dict(),
                status_code=getattr(e, "status_code", 500),
            )
            return await response(scope, receive, send)

    async def handle_websocket(
        self, scope: Scope, receive: Receive, send: Send, **kwargs
    ) -> None:
        """Handle websocket connections."""
        path = scope["path"].rstrip("/")

        try:
            (
                handler,
                kwargs,
                permission_classes,
                throttle_classes,
                _,
                _,
                _,
            ) = await self.match(path, "WEBSOCKET")
        except exceptions.NotFound:
            await send({"type": "websocket.close", "code": 4004})
            return

        if handler is None:
            await send({"type": "websocket.close", "code": 4004})
            return

        await handler(scope, receive, send, **kwargs)

    async def _parse_multipart(self, body: bytes, content_type: str) -> Dict:
        """Parse multipart form data."""
        try:
            import cgi
            from io import BytesIO

            environ = {
                "REQUEST_METHOD": "POST",
                "CONTENT_TYPE": content_type,
                "CONTENT_LENGTH": str(len(body)),
            }

            fp = BytesIO(body)
            fields = {}
            files = {}

            form = cgi.FieldStorage(fp=fp, environ=environ, keep_blank_values=True)

            for key in form.keys():
                item = form[key]
                if item.filename:
                    files[key] = item.file.read()
                else:
                    fields[key] = item.value

            return {"fields": fields, "files": files}
        except Exception as e:
            raise exceptions.ValidationError(f"Invalid multipart form data: {str(e)}")

    async def match(self, path: str, method: str = "GET"):
        """Matches a request path and method to a handler."""
        matched_handlers = []

        for (
            pattern,
            handler,
            allowed_methods,
            permission_classes,
            throttle_classes,
            name,
            original_path,
            skip_body_parsing,
        ) in self.routes:
            match = pattern.match(path)
            if match:
                matched_handlers.append(
                    (
                        handler,
                        allowed_methods,
                        permission_classes,
                        throttle_classes,
                        match,
                        name,
                        original_path,
                        skip_body_parsing,
                    )
                )

        if not matched_handlers:
            # Use default NotFound message for consistency
            raise exceptions.NotFound()

        for (
            handler,
            allowed_methods,
            permission_classes,
            throttle_classes,
            match,
            name,
            original_path,
            skip_body_parsing,
        ) in matched_handlers:
            if method in allowed_methods:
                kwargs = match.groupdict()
                return (
                    handler,
                    {**kwargs},
                    permission_classes,
                    throttle_classes,
                    name,
                    original_path,
                    skip_body_parsing,
                )

        if matched_handlers:
            raise exceptions.MethodNotAllowed(method, path)

        raise exceptions.NotFound()

    @staticmethod
    async def response(
        data, status=200, headers=None, media_type="application/json"
    ):
        """Sends an HTTP response."""
        resp = Response(
            body=data, status_code=status, headers=headers, media_type=media_type
        )
        return resp

    @staticmethod
    async def data(receive: Callable) -> Dict:
        """Extracts data from the request body."""
        raw_data = await receive()
        body = raw_data.get("body", b"")

        if not body:
            return {}

        if isinstance(body, str):
            body = body.encode("utf-8")

        try:
            return json.loads(body)
        except json.JSONDecodeError:
            try:
                return json.loads(body.decode("utf-8"))
            except json.JSONDecodeError as e:
                try:
                    parsed = parse_qs(body.decode("utf-8"))
                    return {k: v[0] if len(v) == 1 else v for k, v in parsed.items()}
                except Exception as e:
                    print(f"Query string parse error: {e}")
                raise ValueError(f"Unable to parse request body: {e}")

    def reverse(self, name: str, **kwargs) -> str:
        """Reverse URL lookup based on endpoint name and provided kwargs."""
        for (
            pattern,
            handler,
            allowed_methods,
            permission_classes,
            throttle_classes,
            route_name,
            original_path,
            skip_body_parsing,
        ) in self.routes:
            if route_name == name:
                url = original_path
                # Correctly extract parameter names and types
                for param_match in re.findall(r"<(\w+):(\w+)>", original_path):
                    param_type, param_name = param_match
                    if param_name in kwargs:
                        url = url.replace(
                            f"<{param_type}:{param_name}>", str(kwargs[param_name])
                        )
                    else:
                        raise ValueError(
                            f"Missing parameter '{param_name}' for route '{name}'."
                        )
                # Check if all parameters have been replaced
                if "<" not in url and ">" not in url:
                    return url
                else:
                    remaining_params = [
                        param_name
                        for _, param_name in re.findall(r"<(\w+):(\w+)>", url)
                    ]
                    raise ValueError(
                        f"Missing parameters for route '{name}'. Required: {remaining_params}"
                    )
        raise ValueError(f"Reverse for '{name}' not found.")

    @staticmethod
    async def handle_lifespan(scope: Dict, receive: Callable, send: Callable) -> None:
        """Handle lifespan protocol events without any global background concerns."""
        while True:
            message = await receive()
            if message["type"] == "lifespan.startup":
                try:
                    await send({"type": "lifespan.startup.complete"})
                except Exception as e:
                    await send({"type": "lifespan.startup.failed", "message": str(e)})
            elif message["type"] == "lifespan.shutdown":
                try:
                    await send({"type": "lifespan.shutdown.complete"})
                except Exception as e:
                    await send({"type": "lifespan.shutdown.failed", "message": str(e)})
                break

# Convenience decorator aliases for simpler imports
# from neutronapi.base import API, endpoint, websocket
endpoint = API.endpoint
websocket = API.websocket
