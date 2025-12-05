import unittest
import os
import tempfile
from neutronapi.db import Model
from neutronapi.db.fields import CharField, JSONField
from neutronapi.db.connection import setup_databases
from neutronapi.db.queryset import Q


class TestObject(Model):
    """Test model for QuerySet testing."""
    key = CharField(null=False)
    name = CharField(null=True)
    kind = CharField(null=True)
    folder = CharField(null=True)  
    parent = CharField(null=True)
    meta = JSONField(null=True, default=dict)
    store = JSONField(null=True, default=dict)
    connections = JSONField(null=True, default=dict)


class TestQuerySetSQLite(unittest.IsolatedAsyncioTestCase):
    def _should_skip_for_provider(self):
        """Skip SQLite-specific tests when running with non-SQLite providers"""
        provider = os.environ.get('DATABASE_PROVIDER', '').lower()
        if provider in ('asyncpg', 'postgres', 'postgresql'):
            self.skipTest('SQLite-specific test skipped when running with PostgreSQL provider')
    
    async def asyncSetUp(self):
        self._should_skip_for_provider()
        
        # Create temporary SQLite database for testing
        self.temp_db = tempfile.NamedTemporaryFile(delete=False, suffix='.db')
        self.temp_db.close()
        
        # Setup database configuration
        db_config = {
            'default': {
                'ENGINE': 'aiosqlite',
                'NAME': self.temp_db.name,
            }
        }
        self.db_manager = setup_databases(db_config)
        
        # Create the table using migration system
        from neutronapi.db.migrations import CreateModel
        connection = await self.db_manager.get_connection()
        
        # Create table for TestObject model using migrations
        create_operation = CreateModel('neutronapi.TestObject', TestObject._neutronapi_fields_)
        await create_operation.database_forwards(
            app_label='neutronapi',
            provider=connection.provider, 
            from_state=None,
            to_state=None,
            connection=connection
        )

    async def asyncTearDown(self):
        """Clean up after each test."""
        await self.db_manager.close_all()
        # Remove temp database file
        try:
            os.unlink(self.temp_db.name)
        except:
            pass

    async def test_crud_and_filters(self):
        # CREATE: Insert test data using Model.objects.create()
        await TestObject.objects.create(
            id="obj-1",
            key="/org-1/files/a.txt", 
            name="A",
            kind="file",
            meta={"tag": "alpha"},
            folder="/org-1/files",
            parent="/org-1"
        )
        await TestObject.objects.create(
            id="obj-2",
            key="/org-1/files/b.txt",
            name="B", 
            kind="file",
            meta={"tag": "beta"},
            folder="/org-1/files",
            parent="/org-1"
        )

        # Test QuerySet operations using Model.objects
        # Count
        count = await TestObject.objects.count()
        self.assertEqual(count, 2)

        # Filter by folder
        folder = '/org-1/files'
        qs_folder = await TestObject.objects.filter(folder=folder)
        results = list(qs_folder)
        self.assertEqual(len(results), 2)

        # Test basic filtering
        qs_alpha = await TestObject.objects.filter(name='A')
        alpha_results = list(qs_alpha)
        self.assertEqual(len(alpha_results), 1)
        self.assertEqual(alpha_results[0].name, 'A')

        # Test first()
        first_result = await TestObject.objects.filter(folder='/org-1/files').first()
        self.assertIsNotNone(first_result)
        self.assertIn(first_result.name, ['A', 'B'])


class TestQuerySetUsing(unittest.IsolatedAsyncioTestCase):
    """Test QuerySet.using() method for multi-database support."""

    async def asyncSetUp(self):
        # Create two temporary SQLite databases
        self.temp_db_default = tempfile.NamedTemporaryFile(delete=False, suffix='.db')
        self.temp_db_default.close()
        self.temp_db_secondary = tempfile.NamedTemporaryFile(delete=False, suffix='.db')
        self.temp_db_secondary.close()

        # Setup database configuration with two databases
        db_config = {
            'default': {
                'ENGINE': 'aiosqlite',
                'NAME': self.temp_db_default.name,
            },
            'secondary': {
                'ENGINE': 'aiosqlite',
                'NAME': self.temp_db_secondary.name,
            }
        }
        self.db_manager = setup_databases(db_config)

        # Create tables in both databases
        from neutronapi.db.migrations import CreateModel

        # Create table in default database
        connection_default = await self.db_manager.get_connection('default')
        create_operation = CreateModel('neutronapi.TestObject', TestObject._neutronapi_fields_)
        await create_operation.database_forwards(
            app_label='neutronapi',
            provider=connection_default.provider,
            from_state=None,
            to_state=None,
            connection=connection_default
        )

        # Create table in secondary database
        connection_secondary = await self.db_manager.get_connection('secondary')
        await create_operation.database_forwards(
            app_label='neutronapi',
            provider=connection_secondary.provider,
            from_state=None,
            to_state=None,
            connection=connection_secondary
        )

    async def asyncTearDown(self):
        await self.db_manager.close_all()
        try:
            os.unlink(self.temp_db_default.name)
        except:
            pass
        try:
            os.unlink(self.temp_db_secondary.name)
        except:
            pass

    async def test_using_returns_cloned_queryset(self):
        """Test that .using() returns a new QuerySet with the alias set."""
        qs = TestObject.objects.all()
        qs_with_alias = qs.using('secondary')

        # Should be a different queryset instance
        self.assertIsNot(qs, qs_with_alias)
        # Original should not have alias set
        self.assertIsNone(qs._db_alias)
        # New queryset should have alias set
        self.assertEqual(qs_with_alias._db_alias, 'secondary')

    async def test_using_queries_correct_database(self):
        """Test that .using() queries the specified database."""
        # Insert data into default database
        await TestObject.objects.create(
            id="default-1",
            key="default-key",
            name="DefaultObject"
        )

        # Insert data into secondary database using .using()
        await TestObject.objects.using('secondary').create(
            id="secondary-1",
            key="secondary-key",
            name="SecondaryObject"
        )

        # Query default database - should find default object
        default_count = await TestObject.objects.count()
        self.assertEqual(default_count, 1)
        default_obj = await TestObject.objects.first()
        self.assertEqual(default_obj.name, "DefaultObject")

        # Query secondary database - should find secondary object
        secondary_count = await TestObject.objects.using('secondary').count()
        self.assertEqual(secondary_count, 1)
        secondary_obj = await TestObject.objects.using('secondary').first()
        self.assertEqual(secondary_obj.name, "SecondaryObject")

    async def test_using_with_filter(self):
        """Test that .using() works with filter()."""
        # Insert into secondary
        await TestObject.objects.using('secondary').create(
            id="sec-1",
            key="key-1",
            name="Alpha"
        )
        await TestObject.objects.using('secondary').create(
            id="sec-2",
            key="key-2",
            name="Beta"
        )

        # Filter on secondary database
        results = await TestObject.objects.using('secondary').filter(name="Alpha")
        self.assertEqual(len(list(results)), 1)
        self.assertEqual(list(results)[0].name, "Alpha")

    async def test_using_with_get(self):
        """Test that .using() works with get()."""
        await TestObject.objects.using('secondary').create(
            id="get-test-1",
            key="unique-key",
            name="UniqueObject"
        )

        obj = await TestObject.objects.using('secondary').get(id="get-test-1")
        self.assertEqual(obj.name, "UniqueObject")

    async def test_using_chaining(self):
        """Test that .using() can be chained with other methods."""
        await TestObject.objects.using('secondary').create(
            id="chain-1",
            key="chain-key-1",
            name="First"
        )
        await TestObject.objects.using('secondary').create(
            id="chain-2",
            key="chain-key-2",
            name="Second"
        )

        # Chain using with filter, order_by, and limit
        results = await TestObject.objects.using('secondary').filter(key__startswith="chain").order_by('name').limit(1)
        result_list = list(results)
        self.assertEqual(len(result_list), 1)
        self.assertEqual(result_list[0].name, "First")

    async def test_using_preserves_alias_through_clone(self):
        """Test that _db_alias is preserved when queryset is cloned."""
        qs = TestObject.objects.using('secondary')
        filtered_qs = qs.filter(name="test")
        ordered_qs = filtered_qs.order_by('name')

        self.assertEqual(qs._db_alias, 'secondary')
        self.assertEqual(filtered_qs._db_alias, 'secondary')
        self.assertEqual(ordered_qs._db_alias, 'secondary')
