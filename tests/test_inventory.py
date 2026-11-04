import json
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from devrelay.errors import RelayError
from devrelay.providers import paginated_inventory


class InventoryTests(unittest.TestCase):
    def setUp(self):
        self.config = SimpleNamespace(repo="example/project", root=Path("/tmp"))

    def page(self, kind, numbers, more=False, cursor=None, nested_more=False):
        field = "labels" if kind == "issues" else "closingIssuesReferences"
        return json.dumps({"data": {"repository": {kind: {
            "nodes": [{"number": number, field: {"nodes": [], "pageInfo": {"hasNextPage": nested_more}}}
                      for number in numbers], "pageInfo": {"hasNextPage": more, "endCursor": cursor}}}}})

    def test_connection_walks_beyond_one_thousand_without_search(self):
        for kind in ("issues", "pullRequests"):
            with self.subTest(kind=kind):
                pages = [self.page(kind, range(i * 100 + 1, i * 100 + 101), True, "cursor-" + str(i)) for i in range(10)]
                pages.append(self.page(kind, [1001]))
                with patch("devrelay.providers.command", side_effect=pages) as command:
                    result = paginated_inventory(self.config, kind)
                self.assertEqual(len(result), 1001)
                self.assertEqual(command.call_count, 11)
                self.assertNotIn("cursor=", " ".join(command.call_args_list[0].args[0]))
                self.assertIn("cursor=cursor-9", command.call_args_list[-1].args[0])
                self.assertIn("--hostname", command.call_args_list[-1].args[0])

    def test_truncated_nested_connections_and_pagination_loops_fail_closed(self):
        cases = [[self.page("issues", [1], nested_more=True)],
                 [self.page("issues", [1], True, "same"), self.page("issues", [2], True, "same")],
                 [self.page("issues", [1], True, "next"), self.page("issues", [1])],
                 [self.page("issues", [], True, "next")],
                 [json.dumps({"errors": [{"message": "unavailable"}]})]]
        for pages in cases:
            with self.subTest(pages=pages), patch("devrelay.providers.command", side_effect=pages), self.assertRaises(RelayError):
                paginated_inventory(self.config, "issues")
