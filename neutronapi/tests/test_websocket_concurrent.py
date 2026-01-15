"""
Tests for concurrent WebSocket connections in Application

This tests that the framework can handle multiple simultaneous WebSocket connections
without interference or blocking.
"""
import unittest
import asyncio
from neutronapi.base import API
from neutronapi.application import Application


class TestWebSocketConcurrent(unittest.IsolatedAsyncioTestCase):
    """Test concurrent websocket connection handling"""

    async def test_multiple_concurrent_connections(self):
        """Test that multiple WebSocket connections can run simultaneously"""

        # Track connection state
        active_connections = []
        connection_order = []

        class SocketAPI(API):
            resource = "/ws"
            name = "socket"

            @API.websocket("/connect")
            async def connect(self, scope, receive, send, **kwargs):
                conn_id = scope.get("query_string", b"").decode()
                print(f"[WS] Connection {conn_id} started")
                connection_order.append(f"start_{conn_id}")
                active_connections.append(conn_id)

                await send({"type": "websocket.accept"})

                # Simulate some async work
                await asyncio.sleep(0.1)

                await send({"type": "websocket.send", "text": f"hello_{conn_id}"})

                # More async work
                await asyncio.sleep(0.1)

                active_connections.remove(conn_id)
                connection_order.append(f"end_{conn_id}")
                print(f"[WS] Connection {conn_id} ended")
                await send({"type": "websocket.close", "code": 1000})

        app = Application(apis=[SocketAPI()])

        async def simulate_connection(conn_id: str):
            """Simulate a WebSocket connection"""
            messages = []

            async def receive():
                return {"type": "websocket.connect"}

            async def send(msg):
                messages.append(msg)

            scope = {
                "type": "websocket",
                "path": "/ws/connect",
                "query_string": conn_id.encode(),
                "headers": [],
            }

            await app(scope, receive, send)
            return messages

        # Run 5 concurrent connections
        print("\n[TEST] Starting 5 concurrent WebSocket connections...")
        tasks = [
            asyncio.create_task(simulate_connection(f"conn_{i}"))
            for i in range(5)
        ]

        results = await asyncio.gather(*tasks)

        print(f"[TEST] Connection order: {connection_order}")
        print(f"[TEST] Results: {len(results)} connections completed")

        # Verify all connections completed successfully
        self.assertEqual(len(results), 5)

        for i, messages in enumerate(results):
            self.assertEqual(len(messages), 3, f"Connection {i} should have 3 messages")
            self.assertEqual(messages[0]["type"], "websocket.accept")
            self.assertEqual(messages[1]["type"], "websocket.send")
            self.assertIn(f"hello_conn_{i}", messages[1]["text"])
            self.assertEqual(messages[2]["type"], "websocket.close")

        # Verify connections ran concurrently (not sequentially)
        # If they ran concurrently, we should see interleaved start/end
        # If sequential, we'd see start_0, end_0, start_1, end_1, ...

        # At minimum, all starts should come before all ends if concurrent
        starts = [x for x in connection_order if x.startswith("start_")]
        ends = [x for x in connection_order if x.startswith("end_")]

        print(f"[TEST] Starts: {starts}")
        print(f"[TEST] Ends: {ends}")

        # Check that connections overlapped (at least some starts happened before some ends)
        first_end_idx = connection_order.index(ends[0]) if ends else len(connection_order)
        starts_before_first_end = sum(1 for x in connection_order[:first_end_idx] if x.startswith("start_"))

        print(f"[TEST] Starts before first end: {starts_before_first_end}")

        # If truly concurrent, multiple connections should start before the first one ends
        self.assertGreater(starts_before_first_end, 1,
            "Expected multiple connections to start before first one ends (concurrent)")

    async def test_connections_with_shared_state(self):
        """Test that connections with shared API state don't interfere"""

        class StatefulAPI(API):
            resource = "/ws"
            name = "stateful"

            # Shared state at API level
            connection_count = 0

            @API.websocket("/connect")
            async def connect(self, scope, receive, send, **kwargs):
                # Increment shared counter
                StatefulAPI.connection_count += 1
                my_count = StatefulAPI.connection_count

                await send({"type": "websocket.accept"})
                await asyncio.sleep(0.05)  # Simulate work
                await send({"type": "websocket.send", "text": f"count_{my_count}"})
                await send({"type": "websocket.close", "code": 1000})

        app = Application(apis=[StatefulAPI()])

        async def simulate_connection():
            messages = []
            async def receive():
                return {"type": "websocket.connect"}
            async def send(msg):
                messages.append(msg)

            scope = {
                "type": "websocket",
                "path": "/ws/connect",
                "query_string": b"",
                "headers": [],
            }
            await app(scope, receive, send)
            return messages

        # Reset counter
        StatefulAPI.connection_count = 0

        # Run 10 concurrent connections
        tasks = [asyncio.create_task(simulate_connection()) for _ in range(10)]
        results = await asyncio.gather(*tasks)

        # All should complete
        self.assertEqual(len(results), 10)

        # Check that counter was incremented correctly
        self.assertEqual(StatefulAPI.connection_count, 10)

        # Each connection should have received a valid count
        counts = []
        for messages in results:
            text = messages[1]["text"]
            count = int(text.split("_")[1])
            counts.append(count)

        # All counts should be unique (1-10)
        self.assertEqual(sorted(counts), list(range(1, 11)))

    async def test_long_running_connections_dont_block(self):
        """Test that a slow connection doesn't block others"""

        class SlowAPI(API):
            resource = "/ws"
            name = "slow"

            @API.websocket("/connect")
            async def connect(self, scope, receive, send, **kwargs):
                is_slow = scope.get("query_string") == b"slow"

                await send({"type": "websocket.accept"})

                if is_slow:
                    await asyncio.sleep(0.5)  # Slow connection
                else:
                    await asyncio.sleep(0.01)  # Fast connection

                await send({"type": "websocket.send", "text": "done"})
                await send({"type": "websocket.close", "code": 1000})

        app = Application(apis=[SlowAPI()])

        async def simulate_connection(query: bytes):
            messages = []
            start_time = asyncio.get_event_loop().time()

            async def receive():
                return {"type": "websocket.connect"}
            async def send(msg):
                messages.append(msg)

            scope = {
                "type": "websocket",
                "path": "/ws/connect",
                "query_string": query,
                "headers": [],
            }
            await app(scope, receive, send)

            end_time = asyncio.get_event_loop().time()
            return messages, end_time - start_time

        # Start slow connection first, then fast ones
        start_time = asyncio.get_event_loop().time()

        slow_task = asyncio.create_task(simulate_connection(b"slow"))

        # Give slow task a head start
        await asyncio.sleep(0.01)

        # Start fast connections
        fast_tasks = [
            asyncio.create_task(simulate_connection(b"fast"))
            for _ in range(5)
        ]

        # Wait for fast connections
        fast_results = await asyncio.gather(*fast_tasks)
        fast_finish_time = asyncio.get_event_loop().time()

        # Wait for slow connection
        slow_result = await slow_task
        slow_finish_time = asyncio.get_event_loop().time()

        # Fast connections should complete in ~0.01s each
        # If blocked by slow, they'd take ~0.5s
        for messages, duration in fast_results:
            self.assertEqual(len(messages), 3)
            self.assertLess(duration, 0.2, "Fast connection took too long - may be blocked")

        # Slow connection should take ~0.5s
        slow_messages, slow_duration = slow_result
        self.assertEqual(len(slow_messages), 3)
        self.assertGreater(slow_duration, 0.4, "Slow connection finished too fast")

        # Fast connections should finish well before slow
        self.assertLess(fast_finish_time - start_time, 0.3)


if __name__ == "__main__":
    unittest.main()
