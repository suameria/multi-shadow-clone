import unittest

from multi_shadow_clone.delivery.application.overview import DeliveryOverview


class DeliveryOverviewTest(unittest.TestCase):
    def test_retired_receipt_is_history_and_payload_does_not_cross_projection(self):
        class Store:
            def read(self):
                return {"stopped": False, "operations": {"op": {"id": "op", "state": "confirmed", "submissions": 2,
                    "reference": {"run_id": "run", "node_id": "final"}, "payload": {"body": "private"},
                    "receipt": {"remote_id": "post"}, "capabilities": {"environment_id": "env"}}}}
        class Environment:
            def state(self): return {"id": "env", "phase": "retired"}
        view = DeliveryOverview(Store(), Environment()).snapshot()
        item = view["operations"][0]
        self.assertEqual(item["state"], "confirmed")
        self.assertFalse(item["environment_current"])
        self.assertNotIn("payload", item)
