#!/usr/bin/env python3
"""Prove the read-only guard refuses every write shape. No network is touched:
these exercise assert_read_only directly."""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import gh_read
from gh_read import GhReadRefused, assert_read_only


class RejectsWrites(unittest.TestCase):
    def test_rejects_post_method(self):
        with self.assertRaises(GhReadRefused):
            assert_read_only(["api", "repos/o/r/issues", "-X", "POST", "-f", "title=x"])

    def test_rejects_patch_method(self):
        with self.assertRaises(GhReadRefused):
            assert_read_only(["api", "repos/o/r/issues/1", "--method", "PATCH", "-f", "state=closed"])

    def test_rejects_method_inline(self):
        with self.assertRaises(GhReadRefused):
            assert_read_only(["api", "repos/o/r", "--method=DELETE"])

    def test_rejects_field_without_explicit_get(self):
        # -f defaults gh to POST; without an explicit -X GET it is a write.
        with self.assertRaises(GhReadRefused):
            assert_read_only(["api", "repos/o/r/labels", "-f", "name=bug"])

    def test_rejects_raw_field_without_get(self):
        with self.assertRaises(GhReadRefused):
            assert_read_only(["api", "repos/o/r/labels", "-F", "name=bug"])

    def test_rejects_input_body(self):
        with self.assertRaises(GhReadRefused):
            assert_read_only(["api", "-X", "GET", "graphql", "--input", "body.json"])

    def test_rejects_graphql_mutation_text(self):
        q = "mutation($id:ID!){addComment(input:{subjectId:$id,body:\"hi\"}){clientMutationId}}"
        with self.assertRaises(GhReadRefused):
            assert_read_only(["api", "graphql", "-f", f"query={q}"])

    def test_rejects_graphql_mutation_nested(self):
        q = "query { x } mutation { y }"
        with self.assertRaises(GhReadRefused):
            assert_read_only(["api", "graphql", "-f", f"query={q}"])

    def test_rejects_graphql_without_query_field(self):
        with self.assertRaises(GhReadRefused):
            assert_read_only(["api", "graphql", "-F", "login=x"])

    def test_rejects_non_api_subcommand(self):
        with self.assertRaises(GhReadRefused):
            assert_read_only(["issue", "create", "--title", "x"])

    def test_rejects_empty(self):
        with self.assertRaises(GhReadRefused):
            assert_read_only([])


class AllowsReads(unittest.TestCase):
    def test_allows_rest_get(self):
        assert_read_only(["api", "-X", "GET", "orgs/example-org/repos", "-f", "per_page=100"])

    def test_allows_rest_get_no_fields(self):
        assert_read_only(["api", "-X", "GET", "users/octocat"])

    def test_allows_search_get(self):
        assert_read_only(["api", "-X", "GET", "search/issues", "-f", "q=org:example-org is:pr", "--jq", ".total_count"])

    def test_allows_graphql_query(self):
        q = "query($login:String!){user(login:$login){id}}"
        assert_read_only(["api", "graphql", "-f", f"query={q}", "-F", "login=octocat"])

    def test_allows_graphql_query_leading_whitespace(self):
        q = "\n  query { viewer { login } }"
        assert_read_only(["api", "graphql", "-f", f"query={q}"])

    def test_field_values_are_not_endpoints(self):
        # A field value that happens to read 'graphql' must not flip the call to
        # the graphql branch and skip the REST GET requirement.
        with self.assertRaises(GhReadRefused):
            assert_read_only(["api", "repos/o/r/x", "-f", "name=graphql"])


class ModuleShape(unittest.TestCase):
    def test_public_helpers_exist(self):
        for name in ("rest_get", "rest_get_all", "graphql", "Search", "token_for", "auth_status_text"):
            self.assertTrue(hasattr(gh_read, name), name)


if __name__ == "__main__":
    unittest.main()
