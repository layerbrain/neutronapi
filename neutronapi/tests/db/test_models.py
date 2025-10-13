import os
import tempfile
import unittest
from neutronapi.db import Model
from neutronapi.db.fields import CharField, JSONField
from neutronapi.db.connection import setup_databases


class TestUser(Model):
    """Test model for database operations."""
    name = CharField(null=False)
    email = CharField(null=False, unique=True)  
    data = JSONField(null=True, default=dict)



class TestModels(unittest.IsolatedAsyncioTestCase):
    """Test cases for Model functionality with actual database operations."""
    
    async def asyncSetUp(self):
        """Set up test database before each test."""
        provider = os.environ.get('DATABASE_PROVIDER', '').lower()
        
        if provider in ('asyncpg', 'postgres', 'postgresql'):
            # Use the existing PostgreSQL test database setup
            from neutronapi.conf import settings
            self.db_manager = setup_databases()
        else:
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
        
        # Create table for TestUser model using migrations
        create_operation = CreateModel('neutronapi.TestUser', TestUser._neutronapi_fields_)
        await create_operation.database_forwards(
            app_label='neutronapi',
            provider=connection.provider, 
            from_state=None,
            to_state=None,
            connection=connection
        )

    async def asyncTearDown(self):
        """Clean up after each test."""
        # Clean up test data
        try:
            await TestUser
            await TestUser.objects.all().delete()
        except Exception:
            pass
            
        await self.db_manager.close_all()
        
        # Remove temp database file if using SQLite
        if hasattr(self, 'temp_db'):
            try:
                os.unlink(self.temp_db.name)
            except:
                pass

    async def test_model_objects_attribute_exists(self):
        """Test that Model.objects exists and has required methods."""
        # Test that objects attribute exists
        self.assertTrue(hasattr(TestUser, 'objects'))
        
        # Test that objects has the essential methods
        self.assertTrue(hasattr(TestUser.objects, 'all'))
        self.assertTrue(hasattr(TestUser.objects, 'filter'))
        self.assertTrue(hasattr(TestUser.objects, 'create'))
        self.assertTrue(hasattr(TestUser.objects, 'get'))

    async def test_model_objects_all_method_works(self):
        """Test that Model.objects.all() returns a QuerySet and can materialize to a list."""
        qs = await TestUser.objects.all()
        # all() awaited returns a QuerySet with methods
        self.assertTrue(hasattr(qs, 'delete'))
        # Materialize by iterating (cache populated by await)
        result = list(qs)
        self.assertIsInstance(result, list)
        self.assertEqual(len(result), 0)

    async def test_model_objects_count_method(self):
        """Test that Model.objects.count() works."""
        count = await TestUser.objects.count()
        self.assertEqual(count, 0)

    async def test_model_objects_filter_returns_queryset(self):
        """Test that Model.objects.filter() returns a QuerySet with proper methods."""
        result = TestUser.objects.filter(name="test")
        
        # Should have QuerySet methods
        self.assertTrue(hasattr(result, 'all'))
        self.assertTrue(hasattr(result, 'first'))
        self.assertTrue(hasattr(result, 'count'))
        
        # Should be able to materialize to a list
        awaited = await result
        all_results = list(awaited)
        self.assertIsInstance(all_results, list)
        
        count = await result.count()
        self.assertEqual(count, 0)

    async def test_model_crud_operations(self):
        """Test complete CRUD operations on the model."""
        # CREATE: Test creating a user
        user_data = {
            'id': 'user-123',
            'name': 'Test User',
            'email': 'test@example.com',
            'data': {'role': 'admin'}
        }
        
        # CREATE: Use Model.objects.create() to insert data
        await TestUser.objects.create(
            id=user_data['id'],
            name=user_data['name'],
            email=user_data['email'],
            data=user_data['data']
        )
        
        # READ: Test that we can fetch the user
        qs_all = await TestUser.objects.all()
        all_users = list(qs_all)
        self.assertEqual(len(all_users), 1)
        
        user = all_users[0]
        self.assertEqual(user.id, 'user-123')
        self.assertEqual(user.name, 'Test User')
        self.assertEqual(user.email, 'test@example.com')
        
        # Test filter
        qs_filt = await TestUser.objects.filter(name='Test User')
        filtered_users = list(qs_filt)
        self.assertEqual(len(filtered_users), 1)
        self.assertEqual(filtered_users[0].email, 'test@example.com')
        
        # Test count
        count = await TestUser.objects.count()
        self.assertEqual(count, 1)
        
        # Test get (should work if only one result)
        single_user = await TestUser.objects.filter(email='test@example.com').first()
        self.assertIsNotNone(single_user)
        self.assertEqual(single_user.name, 'Test User')

    async def test_model_does_not_exist_exception(self):
        """Test that Model.DoesNotExist exception is properly implemented."""
        # Test that DoesNotExist is defined on the model class
        self.assertTrue(hasattr(TestUser, 'DoesNotExist'))
        self.assertTrue(issubclass(TestUser.DoesNotExist, Exception))
        
        # Test that DoesNotExist is raised when using get() with no results
        with self.assertRaises(TestUser.DoesNotExist):
            await TestUser.objects.get(name='nonexistent')
        
        # Test that different models have different DoesNotExist classes
        class AnotherModel(Model):
            name = CharField()
        
        self.assertNotEqual(TestUser.DoesNotExist, AnotherModel.DoesNotExist)
        self.assertTrue(issubclass(AnotherModel.DoesNotExist, Exception))
