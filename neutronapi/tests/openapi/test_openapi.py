import unittest

from neutronapi.base import API
from neutronapi.openapi.openapi import OpenAPIGenerator


class PingAPI(API):
    name = "ping"
    resource = ""

    @API.endpoint("/ping", methods=["GET"], name="ping")
    async def ping(self, scope, receive, send):
        return await self.response({"ok": True})


# Test APIs for comprehensive discovery testing
class HealthAPI(API):
    resource = "/v1/health"
    name = "health"

    @API.endpoint("/", methods=["GET"], name="get")
    async def get(self, scope, receive, send, **kwargs):
        return await self.response({"status": "ok", "version": "v1"})


class UsersAPI(API):
    resource = "/v1/users"
    name = "users"
    
    @API.endpoint("/", methods=["GET"], name="list")
    async def list_users(self, scope, receive, send, **kwargs):
        return await self.response({"users": []})
    
    @API.endpoint("/", methods=["POST"], name="create") 
    async def create_user(self, scope, receive, send, **kwargs):
        return await self.response({"id": "123"})
    
    @API.endpoint("/<int:user_id>", methods=["GET"], name="get")
    async def get_user(self, scope, receive, send, user_id=None, **kwargs):
        return await self.response({"id": user_id})
    
    @API.endpoint("/<int:user_id>", methods=["PUT"], name="update")
    async def update_user(self, scope, receive, send, user_id=None, **kwargs):
        return await self.response({"id": user_id})
    
    @API.endpoint("/<int:user_id>", methods=["DELETE"], name="delete")
    async def delete_user(self, scope, receive, send, user_id=None, **kwargs):
        return await self.response(None, status=204)


class ProductsAPI(API):
    resource = "/v1/products" 
    name = "products"
    
    @API.endpoint("/", methods=["GET"], name="list")
    async def list_products(self, scope, receive, send, **kwargs):
        return await self.response({"products": []})
    
    @API.endpoint("/", methods=["POST"], name="create")
    async def create_product(self, scope, receive, send, **kwargs):
        return await self.response({"id": "456"})
    
    @API.endpoint("/<int:product_id>", methods=["GET"], name="get")
    async def get_product(self, scope, receive, send, product_id=None, **kwargs):
        return await self.response({"id": product_id})
    
    @API.endpoint("/<int:product_id>/reviews", methods=["GET"], name="reviews")
    async def get_reviews(self, scope, receive, send, product_id=None, **kwargs):
        return await self.response({"reviews": []})


class HiddenAPI(API):
    resource = "/v1/internal"
    name = "internal"
    hidden = True  # This should be excluded from docs
    
    @API.endpoint("/debug", methods=["GET"], name="debug")
    async def debug(self, scope, receive, send, **kwargs):
        return await self.response({"debug": True})


class ExcludeEndpointAPI(API):
    resource = "/v1/mixed"
    name = "mixed"
    
    @API.endpoint("/public", methods=["GET"], name="public")
    async def public(self, scope, receive, send, **kwargs):
        return await self.response({"public": True})
    
    @API.endpoint("/private", methods=["GET"], name="private", include_in_docs=False)
    async def private(self, scope, receive, send, **kwargs):
        return await self.response({"private": True})


class TestOpenAPI(unittest.IsolatedAsyncioTestCase):
    async def test_generate_from_api(self):
        gen = OpenAPIGenerator(title="Test", description="D", version="1.0.0")
        spec = await gen.generate_from_api(PingAPI())
        self.assertIn("paths", spec)
        # Ensure our route is present
        self.assertIn("/ping", spec.get("paths", {}))

    async def test_comprehensive_endpoint_discovery(self):
        """Test that all endpoints from multiple APIs are discovered"""
        apis = {
            "health": HealthAPI(),
            "users": UsersAPI(),
            "products": ProductsAPI(),
        }
        
        # Test individual API discovery
        for name, api in apis.items():
            gen = OpenAPIGenerator(title=f"{name.title()} API", version="1.0.0")
            spec = await gen.generate_from_api(api)
            
            # Verify paths are discovered
            paths = spec.get("paths", {})
            self.assertGreater(len(paths), 0, f"{name} API should have discovered paths")
            
            # Verify operations are discovered
            total_operations = sum(len(ops) for ops in paths.values())
            self.assertGreater(total_operations, 0, f"{name} API should have discovered operations")

    async def test_multiple_api_discovery_with_generate(self):
        """Test discovery of multiple APIs using the generate() method"""
        apis = {
            "health": HealthAPI(),
            "users": UsersAPI(), 
            "products": ProductsAPI(),
        }
        
        gen = OpenAPIGenerator(title="Combined API", description="All APIs", version="1.0.0")
        spec = await gen.generate(source=apis)
        
        # Verify all expected paths are present
        expected_paths = [
            "/v1/health/",
            "/v1/users/", 
            "/v1/users/{user_id}",
            "/v1/products/",
            "/v1/products/{product_id}",
            "/v1/products/{product_id}/reviews"
        ]
        
        actual_paths = list(spec["paths"].keys())
        
        for expected_path in expected_paths:
            self.assertIn(expected_path, actual_paths, f"Expected path {expected_path} not found")
        
        # Verify total operations count
        total_operations = sum(len(ops) for ops in spec["paths"].values())
        expected_operations = 1 + 5 + 4  # health(1) + users(5) + products(4) 
        self.assertEqual(total_operations, expected_operations, "Total operations count mismatch")

    async def test_manual_process_api_method(self):
        """Test manual _process_api calls for multiple APIs"""
        apis = [HealthAPI(), UsersAPI(), ProductsAPI()]
        
        gen = OpenAPIGenerator(title="Manual Process", version="1.0.0")
        for api in apis:
            await gen._process_api(api)
        
        spec = gen.to_dict()
        
        # Should have all paths
        self.assertIn("/v1/health/", spec["paths"])
        self.assertIn("/v1/users/", spec["paths"]) 
        self.assertIn("/v1/users/{user_id}", spec["paths"])
        self.assertIn("/v1/products/", spec["paths"])
        
        # Verify operations
        total_operations = sum(len(ops) for ops in spec["paths"].values())
        self.assertEqual(total_operations, 10)  # 1 + 5 + 4

    async def test_hidden_api_exclusion(self):
        """Test that hidden APIs are excluded from documentation"""
        gen = OpenAPIGenerator(title="Test Hidden", version="1.0.0")
        spec = await gen.generate_from_api(HiddenAPI())
        
        # Hidden API should result in no paths
        self.assertEqual(len(spec["paths"]), 0, "Hidden API should not contribute any paths")

    async def test_endpoint_include_in_docs_exclusion(self):
        """Test that endpoints with include_in_docs=False are excluded"""
        gen = OpenAPIGenerator(title="Test Exclude Endpoint", version="1.0.0")
        spec = await gen.generate_from_api(ExcludeEndpointAPI())
        
        paths = spec["paths"]
        # Should only have public endpoint
        self.assertIn("/v1/mixed/public", paths)
        self.assertNotIn("/v1/mixed/private", paths)
        self.assertEqual(len(paths), 1, "Should only have 1 path (public)")

    async def test_all_http_methods_discovery(self):
        """Test that all HTTP methods are properly discovered"""
        gen = OpenAPIGenerator(title="Test Methods", version="1.0.0")
        spec = await gen.generate_from_api(UsersAPI())
        
        users_list_path = spec["paths"]["/v1/users/"]
        users_detail_path = spec["paths"]["/v1/users/{user_id}"]
        
        # List endpoint should have GET and POST
        self.assertIn("get", users_list_path)
        self.assertIn("post", users_list_path)
        
        # Detail endpoint should have GET, PUT, DELETE
        self.assertIn("get", users_detail_path)
        self.assertIn("put", users_detail_path)
        self.assertIn("delete", users_detail_path)

    async def test_endpoint_metadata_preservation(self):
        """Test that endpoint metadata is preserved in OpenAPI spec"""
        gen = OpenAPIGenerator(title="Test Metadata", version="1.0.0")
        spec = await gen.generate_from_api(UsersAPI())
        
        # Check that operation IDs are generated correctly
        users_list_get = spec["paths"]["/v1/users/"]["get"]
        self.assertEqual(users_list_get["operationId"], "users_list_get")
        
        users_list_post = spec["paths"]["/v1/users/"]["post"]
        self.assertEqual(users_list_post["operationId"], "users_create_post")
        
        # Check tags are applied
        self.assertEqual(users_list_get["tags"], ["Users"])

    async def test_path_parameter_conversion(self):
        """Test that path parameters are converted to OpenAPI format"""
        gen = OpenAPIGenerator(title="Test Params", version="1.0.0")  
        spec = await gen.generate_from_api(UsersAPI())
        
        # Should convert <int:user_id> to {user_id}
        self.assertIn("/v1/users/{user_id}", spec["paths"])
        self.assertNotIn("/v1/users/<int:user_id>", spec["paths"])

    async def test_granular_discovery_options(self):
        """Test granular options for endpoint discovery"""
        # Test default behavior (should get all non-hidden, non-excluded endpoints)
        apis = {
            "health": HealthAPI(),
            "users": UsersAPI(),
            "products": ProductsAPI(),
            "hidden": HiddenAPI(),
            "mixed": ExcludeEndpointAPI(),
        }
        
        gen = OpenAPIGenerator(title="Test Granular", version="1.0.0")
        spec = await gen.generate(source=apis)
        
        paths = spec["paths"]
        
        # Should include all public endpoints
        self.assertIn("/v1/health/", paths)
        self.assertIn("/v1/users/", paths)
        self.assertIn("/v1/products/", paths)
        self.assertIn("/v1/mixed/public", paths)
        
        # Should exclude hidden API and private endpoint  
        self.assertNotIn("/v1/internal/debug", paths)
        self.assertNotIn("/v1/mixed/private", paths)
        
        # Count total operations
        total_operations = sum(len(ops) for ops in paths.values())
        expected_operations = 1 + 5 + 4 + 1  # health + users + products + mixed public
        self.assertEqual(total_operations, expected_operations)

    async def test_include_all_option(self):
        """Test include_all option"""
        apis = {
            "health": HealthAPI(),
            "hidden": HiddenAPI(),
        }
        
        # Default behavior - exclude hidden APIs
        gen_default = OpenAPIGenerator(title="Default", version="1.0.0")
        spec_default = await gen_default.generate(source=apis)
        self.assertIn("/v1/health/", spec_default["paths"])
        self.assertNotIn("/v1/internal/debug", spec_default["paths"])
        
        # With include_all=True - include hidden APIs
        gen_include = OpenAPIGenerator(
            title="Include All", 
            version="1.0.0", 
            include_all=True
        )
        spec_include = await gen_include.generate(source=apis)
        self.assertIn("/v1/health/", spec_include["paths"])
        self.assertIn("/v1/internal/debug", spec_include["paths"])

    async def test_exclude_patterns_option(self):
        """Test exclude_patterns option"""
        apis = {
            "health": HealthAPI(),
            "users": UsersAPI(),
        }
        
        # Exclude health endpoints with pattern  
        gen = OpenAPIGenerator(
            title="Test Exclude",
            version="1.0.0",
            exclude_patterns=["/v1/health/"]  # Exact match
        )
        spec = await gen.generate(source=apis)
        
        # Should exclude health API but include users API
        self.assertNotIn("/v1/health/", spec["paths"])
        self.assertIn("/v1/users/", spec["paths"])
        
        # Test multiple patterns
        gen2 = OpenAPIGenerator(
            title="Test Exclude Multiple",
            version="1.0.0", 
            exclude_patterns=["/v1/health/", "/v1/users/{user_id}"]
        )
        spec2 = await gen2.generate(source=apis)
        
        # Should exclude health and user detail endpoints
        self.assertNotIn("/v1/health/", spec2["paths"])
        self.assertIn("/v1/users/", spec2["paths"])  # List endpoint should remain
        self.assertNotIn("/v1/users/{user_id}", spec2["paths"])  # Detail endpoint excluded

    async def test_generate_all_endpoints_convenience_function(self):
        """Test the generate_all_endpoints_openapi convenience function"""
        from neutronapi.openapi.openapi import generate_all_endpoints_openapi

        apis = {
            "health": HealthAPI(),
            "hidden": HiddenAPI(),
            "mixed": ExcludeEndpointAPI(),
        }

        spec = await generate_all_endpoints_openapi(apis, title="All Endpoints")

        # Should include ALL endpoints, even hidden ones and private ones
        self.assertIn("/v1/health/", spec["paths"])
        self.assertIn("/v1/internal/debug", spec["paths"])  # Hidden API included
        self.assertIn("/v1/mixed/public", spec["paths"])
        self.assertIn("/v1/mixed/private", spec["paths"])  # Private endpoint included

    async def test_path_parameters_in_operation_parameters(self):
        """Test that path parameters appear in operation parameters alongside pagination params"""
        # Create API with path parameter and custom endpoint metadata
        class PathParamAPI(API):
            resource = "/v1/items"
            name = "items"

            @API.endpoint(
                "/<int:item_id>",
                methods=["GET"],
                name="get",
                parameters=[
                    {"name": "item_id", "in": "path", "required": True, "schema": {"type": "integer"}}
                ]
            )
            async def get_item(self, scope, receive, send, item_id=None, **kwargs):
                return await self.response({"id": item_id})

        gen = OpenAPIGenerator(title="Test", version="1.0.0")
        spec = await gen.generate_from_api(PathParamAPI())

        item_detail = spec["paths"]["/v1/items/{item_id}"]["get"]
        param_names = [p["name"] for p in item_detail.get("parameters", [])]

        # Should have path param (from custom metadata)
        self.assertIn("item_id", param_names)
        # Should also have pagination params (merged, not overwritten)
        self.assertIn("page", param_names)
        self.assertIn("page_size", param_names)
        self.assertIn("ordering", param_names)

    async def test_response_schema_wrapped_in_content(self):
        """Test that response schemas are properly wrapped in content structure"""
        # Create API with custom response using top-level schema (old/incorrect format)
        class CustomResponseAPI(API):
            resource = "/v1/test"
            name = "test"

            @API.endpoint(
                "/",
                methods=["POST"],
                name="create",
                responses={
                    201: {"description": "Created", "schema": {"type": "object", "properties": {"id": {"type": "string"}}}}
                }
            )
            async def create(self, scope, receive, send, **kwargs):
                return await self.response({"id": "123"}, status=201)

        gen = OpenAPIGenerator(title="Test", version="1.0.0")
        spec = await gen.generate_from_api(CustomResponseAPI())

        response_201 = spec["paths"]["/v1/test/"]["post"]["responses"]["201"]
        # Should be wrapped in content
        self.assertIn("content", response_201)
        self.assertIn("application/json", response_201["content"])
        self.assertIn("schema", response_201["content"]["application/json"])
        # Should NOT have schema at top level
        self.assertNotIn("schema", response_201)

    async def test_paginated_false_excludes_pagination_params(self):
        """Test that paginated=False excludes pagination parameters from OpenAPI spec"""
        class NonPaginatedAPI(API):
            resource = "/v1/items"
            name = "items"

            @API.endpoint(
                "/<int:item_id>",
                methods=["GET"],
                name="get",
                paginated=False,  # Explicitly disable pagination params
                parameters=[
                    {"name": "item_id", "in": "path", "required": True, "schema": {"type": "integer"}}
                ]
            )
            async def get_item(self, scope, receive, send, item_id=None, **kwargs):
                return await self.response({"id": item_id})

        gen = OpenAPIGenerator(title="Test", version="1.0.0")
        spec = await gen.generate_from_api(NonPaginatedAPI())

        item_detail = spec["paths"]["/v1/items/{item_id}"]["get"]
        param_names = [p["name"] for p in item_detail.get("parameters", [])]

        # Should have path param
        self.assertIn("item_id", param_names)
        # Should NOT have pagination params
        self.assertNotIn("page", param_names)
        self.assertNotIn("page_size", param_names)
        self.assertNotIn("ordering", param_names)

    async def test_paginated_true_includes_pagination_params(self):
        """Test that paginated=True (default) includes pagination parameters"""
        class PaginatedAPI(API):
            resource = "/v1/items"
            name = "items"

            @API.endpoint(
                "/",
                methods=["GET"],
                name="list",
                paginated=True,  # Explicitly enable (also the default)
            )
            async def list_items(self, scope, receive, send, **kwargs):
                return await self.response({"data": []})

        gen = OpenAPIGenerator(title="Test", version="1.0.0")
        spec = await gen.generate_from_api(PaginatedAPI())

        list_endpoint = spec["paths"]["/v1/items/"]["get"]
        param_names = [p["name"] for p in list_endpoint.get("parameters", [])]

        # Should have pagination params
        self.assertIn("page", param_names)
        self.assertIn("page_size", param_names)
        self.assertIn("ordering", param_names)

