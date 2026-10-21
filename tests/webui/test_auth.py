import base64
import os
import sys
import unittest
from pathlib import Path


sys.path.insert(0, str(Path(__file__).parents[2]))
sys.path.insert(0, str(Path(__file__).parents[2] / "src"))

from starlette.applications import Starlette
from starlette.responses import PlainTextResponse
from starlette.routing import Route
from starlette.testclient import TestClient

from webui.auth import (BasicAuthMiddleware, Credentials, auth_warning,
                         credentials_from_env)


async def hello(_request):
    return PlainTextResponse("ok")


def _client(credentials: Credentials) -> TestClient:
    app = Starlette(routes=[Route("/", hello)])
    app.add_middleware(BasicAuthMiddleware, credentials=credentials)
    return TestClient(app)


def _header(username: str, password: str) -> dict[str, str]:
    raw = base64.b64encode(f"{username}:{password}".encode()).decode()
    return {"Authorization": f"Basic {raw}"}


class BasicAuthTests(unittest.TestCase):
    def test_request_without_credentials_is_challenged(self) -> None:
        client = _client(Credentials("admin", "secret"))
        response = client.get("/")
        self.assertEqual(response.status_code, 401)
        self.assertIn("Basic", response.headers["www-authenticate"])

    def test_request_with_the_right_credentials_passes(self) -> None:
        client = _client(Credentials("admin", "secret"))
        response = client.get("/", headers=_header("admin", "secret"))
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.text, "ok")

    def test_wrong_password_is_challenged(self) -> None:
        client = _client(Credentials("admin", "secret"))
        response = client.get("/", headers=_header("admin", "wrong"))
        self.assertEqual(response.status_code, 401)

    def test_wrong_user_is_challenged(self) -> None:
        client = _client(Credentials("admin", "secret"))
        response = client.get("/", headers=_header("root", "secret"))
        self.assertEqual(response.status_code, 401)

    def test_malformed_header_is_challenged(self) -> None:
        client = _client(Credentials("admin", "secret"))
        response = client.get("/", headers={"Authorization": "Basic !!!!"})
        self.assertEqual(response.status_code, 401)

    def test_other_scheme_is_challenged(self) -> None:
        client = _client(Credentials("admin", "secret"))
        response = client.get("/", headers={"Authorization": "Bearer secret"})
        self.assertEqual(response.status_code, 401)


class CredentialsFromEnvTests(unittest.TestCase):
    def setUp(self) -> None:
        self._saved = {name: os.environ.pop(name, None)
                       for name in ("AGENT_RUNTIME_WEBUI_AUTH_USER",
                                    "AGENT_RUNTIME_WEBUI_AUTH_PASSWORD")}

    def tearDown(self) -> None:
        for name, value in self._saved.items():
            if value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = value

    def test_nothing_configured_means_no_credentials(self) -> None:
        self.assertIsNone(credentials_from_env())

    def test_password_alone_does_not_enable_authentication(self) -> None:
        os.environ["AGENT_RUNTIME_WEBUI_AUTH_PASSWORD"] = "secret"
        self.assertIsNone(credentials_from_env())

    def test_user_name_alone_does_not_enable_authentication(self) -> None:
        os.environ["AGENT_RUNTIME_WEBUI_AUTH_USER"] = "admin"
        self.assertIsNone(credentials_from_env())

    def test_user_name_and_password_enable_authentication(self) -> None:
        os.environ["AGENT_RUNTIME_WEBUI_AUTH_USER"] = "jun"
        os.environ["AGENT_RUNTIME_WEBUI_AUTH_PASSWORD"] = "secret"
        credentials = credentials_from_env()
        self.assertIsNotNone(credentials)
        self.assertEqual(credentials.username, "jun")
        self.assertEqual(credentials.password, "secret")

    def test_no_warning_when_authentication_is_on(self) -> None:
        os.environ["AGENT_RUNTIME_WEBUI_AUTH_USER"] = "jun"
        os.environ["AGENT_RUNTIME_WEBUI_AUTH_PASSWORD"] = "secret"
        self.assertIsNone(auth_warning())

    def test_warning_when_nothing_is_configured(self) -> None:
        warning = auth_warning()
        self.assertIsNotNone(warning)
        self.assertIn("neither", warning)

    def test_warning_names_the_missing_password(self) -> None:
        os.environ["AGENT_RUNTIME_WEBUI_AUTH_USER"] = "jun"
        warning = auth_warning()
        self.assertIsNotNone(warning)
        self.assertIn("AGENT_RUNTIME_WEBUI_AUTH_PASSWORD", warning)

    def test_warning_names_the_missing_user_name(self) -> None:
        os.environ["AGENT_RUNTIME_WEBUI_AUTH_PASSWORD"] = "secret"
        warning = auth_warning()
        self.assertIsNotNone(warning)
        self.assertIn("AGENT_RUNTIME_WEBUI_AUTH_USER", warning)


if __name__ == "__main__":
    unittest.main()
