import importlib.util
import io
import json
import os
from pathlib import Path
import stat
import tempfile
import unittest
from unittest import mock


MODULE_PATH = Path(__file__).with_name("kc_lockout.py")
SPEC = importlib.util.spec_from_file_location("kc_lockout", MODULE_PATH)
kc = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
SPEC.loader.exec_module(kc)


def json_response(status, body):
    return kc.HttpResponse(status, {"Content-Type": "application/json"}, json.dumps(body).encode())


class FakeTransport:
    def __init__(self, handler):
        self.handler = handler
        self.calls = []

    def request(self, method, url, *, headers=None, data=None):
        self.calls.append(
            {
                "method": method,
                "url": url,
                "headers": dict(headers or {}),
                "data": data,
            }
        )
        return self.handler(method, url, headers or {}, data)


def normalized_target(desired=None):
    return kc.validate_config(
        {
            "targets": [
                {
                    "name": "demo",
                    "base_url": "https://keycloak.example.test",
                    "realm": "demo",
                    "token_env": "KC_TEST_TOKEN",
                    "desired": desired or {"bruteForceProtected": True, "failureFactor": 12},
                }
            ]
        }
    )[0]


class LockoutToolTests(unittest.TestCase):
    def test_diff_reports_only_configured_brute_force_fields(self):
        current = {
            "bruteForceProtected": False,
            "failureFactor": 30,
            "waitIncrementSeconds": 60,
            "notBruteForce": "preserve",
        }
        desired = {"bruteForceProtected": True, "failureFactor": 12}

        self.assertEqual(
            kc.diff_brute_force(current, desired),
            [
                ("bruteForceProtected", False, True),
                ("failureFactor", 30, 12),
            ],
        )

    def test_apply_put_merges_full_representation_and_only_changes_brute_keys(self):
        before = {
            "id": "demo-id",
            "realm": "demo",
            "enabled": True,
            "resetPasswordAllowed": True,
            "smtpServer": {"host": "smtp.example.test", "port": "587"},
            "bruteForceProtected": False,
            "failureFactor": 30,
            "permanentLockout": False,
            "maxTemporaryLockouts": 0,
            "waitIncrementSeconds": 60,
            "customSetting": {"keep": ["this"]},
        }
        state = {"realm": before}

        def handler(method, url, _headers, data):
            if method == "GET":
                return json_response(200, state["realm"])
            if method == "PUT":
                state["realm"] = json.loads(data.decode())
                return kc.HttpResponse(204)
            raise AssertionError(f"unexpected {method} {url}")

        transport = FakeTransport(handler)
        target = normalized_target(
            {
                "bruteForceProtected": True,
                "failureFactor": 12,
                "waitIncrementSeconds": 120,
            }
        )
        output = io.StringIO()
        with tempfile.TemporaryDirectory() as directory, mock.patch.dict(
            os.environ, {"KC_TEST_TOKEN": "test-token"}, clear=False
        ):
            result = kc.apply_target(target, transport, output, backup_dir=Path(directory))
            self.assertEqual(result, 0)
            backups = list(Path(directory).glob("demo-*.json"))
            self.assertEqual(len(backups), 1)
            self.assertEqual(stat.S_IMODE(backups[0].stat().st_mode), 0o600)
            self.assertEqual(json.loads(backups[0].read_text()), before)

        self.assertEqual([call["method"] for call in transport.calls], ["GET", "PUT", "GET"])
        put_body = json.loads(transport.calls[1]["data"].decode())
        self.assertEqual(put_body["customSetting"], before["customSetting"])
        self.assertEqual(put_body["smtpServer"], before["smtpServer"])
        for key, value in before.items():
            if key in {"bruteForceProtected", "failureFactor", "waitIncrementSeconds"}:
                continue
            self.assertEqual(put_body[key], value)
        self.assertTrue(put_body["bruteForceProtected"])
        self.assertEqual(put_body["failureFactor"], 12)
        self.assertEqual(put_body["waitIncrementSeconds"], 120)

    def test_plan_exit_codes_are_zero_for_noop_and_two_for_changes(self):
        current = {
            "bruteForceProtected": False,
            "failureFactor": 30,
            "resetPasswordAllowed": True,
            "smtpServer": {"host": "smtp.example.test"},
        }

        def handler(_method, _url, _headers, _data):
            return json_response(200, current)

        transport = FakeTransport(handler)
        target_file = None
        with tempfile.TemporaryDirectory() as directory, mock.patch.dict(
            os.environ, {"KC_TEST_TOKEN": "test-token"}, clear=False
        ):
            target_file = Path(directory) / "targets.json"
            target_file.write_text(
                json.dumps(
                    {
                        "targets": [
                            {
                                "name": "demo",
                                "base_url": "https://keycloak.example.test",
                                "realm": "demo",
                                "token_env": "KC_TEST_TOKEN",
                                "desired": {"bruteForceProtected": True, "failureFactor": 12},
                            }
                        ]
                    }
                )
            )
            output = io.StringIO()
            errors = io.StringIO()
            changed_result = kc.main(
                ["plan", "--targets", str(target_file)],
                transport=transport,
                output=output,
                error_output=errors,
            )
            self.assertEqual(changed_result, 2)
            self.assertIn("Plan requires changes", output.getvalue())
            self.assertEqual(errors.getvalue(), "")

            current.update({"bruteForceProtected": True, "failureFactor": 12})
            output = io.StringIO()
            noop_result = kc.main(
                ["plan", "--targets", str(target_file)],
                transport=transport,
                output=output,
                error_output=errors,
            )
            self.assertEqual(noop_result, 0)
            self.assertIn("Plan is already satisfied", output.getvalue())

        no_yes = kc.main(
            ["apply", "--targets", str(target_file), "--realm", "demo"],
            transport=transport,
            output=io.StringIO(),
            error_output=io.StringIO(),
        )
        self.assertEqual(no_yes, 2)

    def test_unlock_path_precheck_warns_for_missing_reset_and_smtp(self):
        self.assertEqual(
            kc.unlock_path_warnings(
                {"resetPasswordAllowed": True, "smtpServer": {"host": "smtp.example.test"}}
            ),
            [],
        )
        warnings = kc.unlock_path_warnings({"resetPasswordAllowed": False, "smtpServer": {}})
        self.assertEqual(len(warnings), 2)
        self.assertTrue(any("reset-credentials" in warning for warning in warnings))
        self.assertTrue(any("smtpServer.host" in warning for warning in warnings))

        def handler(_method, _url, _headers, _data):
            return json_response(
                200,
                {"bruteForceProtected": True, "resetPasswordAllowed": False, "smtpServer": {}},
            )

        with mock.patch.dict(os.environ, {"KC_TEST_TOKEN": "test-token"}, clear=False):
            output = io.StringIO()
            result = kc.plan_targets([normalized_target({"bruteForceProtected": True})], FakeTransport(handler), output)
        self.assertEqual(result, 0)
        self.assertIn("WARNING demo (demo):", output.getvalue())

    def test_verify_uses_fake_user_probe_and_always_clears_state(self):
        target = normalized_target({"failureFactor": 2})

        def handler(method, url, _headers, data):
            if method == "GET" and "/users?" in url:
                return json_response(200, [{"id": "user-id", "username": "probe"}])
            if method == "POST":
                self.assertNotIn(b"test-token", data)
                return json_response(
                    400,
                    {"error": "invalid_grant", "error_description": "Account temporarily disabled"},
                )
            if method == "GET" and "/attack-detection/" in url:
                return json_response(200, {"disabled": True, "numFailures": 3})
            if method == "DELETE":
                return kc.HttpResponse(204)
            raise AssertionError(f"unexpected {method} {url}")

        transport = FakeTransport(handler)
        with mock.patch.dict(os.environ, {"KC_TEST_TOKEN": "test-token"}, clear=False):
            output = io.StringIO()
            result = kc.verify_target(
                target,
                transport,
                output,
                username="probe",
                attempts=3,
            )
        self.assertEqual(result, 0)
        self.assertIn("Lockout evidence", output.getvalue())
        self.assertIn("Attack-detection state cleared", output.getvalue())
        self.assertEqual(
            [call["method"] for call in transport.calls],
            ["GET", "POST", "POST", "POST", "GET", "DELETE"],
        )

    def test_yaml_subset_loads_target_shape(self):
        yaml = """
targets:
  - name: demo
    base_url: https://keycloak.example.test
    realm: demo
    token_env: KC_TEST_TOKEN
    desired:
      bruteForceProtected: true
      failureFactor: 12
"""
        with tempfile.NamedTemporaryFile("w", suffix=".yaml", delete=False) as file:
            file.write(yaml)
            path = file.name
        try:
            targets = kc.load_config(path)
        finally:
            Path(path).unlink()
        self.assertEqual(targets[0]["name"], "demo")
        self.assertEqual(targets[0]["desired"]["failureFactor"], 12)


if __name__ == "__main__":
    unittest.main()


class UserAgentTest(unittest.TestCase):
    def test_transport_sends_a_named_user_agent(self):
        seen = {}

        def fake_urlopen(req, timeout=None):
            seen["ua"] = req.get_header("User-agent")
            raise kc.urllib.error.URLError("stop")

        with mock.patch.object(kc.urllib.request, "urlopen", fake_urlopen):
            with self.assertRaises(kc.KcError):
                kc.UrllibTransport().request("GET", "http://keycloak.invalid/realms/master")
        self.assertEqual(seen["ua"], kc.USER_AGENT)
