"""F12: exact-origin CORS and readable response headers (app/cors.py).

CORS is a browser read policy, not authentication: these tests also check that it never
changes what the API answers. No database is touched (the app opens no connections and the
authentication seam refuses before any query).
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError
from pydantic_settings import SettingsError

from app.config import Settings
from app.cors import ALLOWED_HEADERS, ALLOWED_METHODS, EXPOSED_HEADERS
from app.main import create_app

WEB = "http://localhost:3000"
PROTECTED = "/api/v1/courses"


def settings(origins: list[str], environment: str = "development") -> Settings:
    return Settings(_env_file=None, environment=environment, cors_allowed_origins=origins)


def client(origins: list[str] | None = None) -> TestClient:
    return TestClient(create_app(settings([WEB] if origins is None else origins)))


def cors_headers(response) -> dict[str, str]:
    return {key: value for key, value in response.headers.items() if key.startswith("access-control-")}


def preflight(test_client: TestClient, origin: str, method: str = "POST", headers: str | None = None):
    request = {"Origin": origin, "Access-Control-Request-Method": method}
    if headers is not None:
        request["Access-Control-Request-Headers"] = headers
    return test_client.options(PROTECTED, headers=request)


# --- configuration ---------------------------------------------------------------------------------


def test_default_allows_no_origin():
    assert Settings(_env_file=None).cors_allowed_origins == []


def test_environment_variable_is_a_json_array(monkeypatch):
    monkeypatch.setenv("CORS_ALLOWED_ORIGINS", '["http://localhost:3000", "https://app.example.edu"]')
    assert Settings(_env_file=None).cors_allowed_origins == [WEB, "https://app.example.edu"]


@pytest.mark.parametrize("raw", ["http://localhost:3000", "http://a.example,http://b.example", "*", "[", ""])
def test_environment_variable_that_is_not_a_json_array_is_refused(monkeypatch, raw):
    monkeypatch.setenv("CORS_ALLOWED_ORIGINS", raw)
    with pytest.raises((SettingsError, ValidationError)):
        Settings(_env_file=None)


@pytest.mark.parametrize("raw", ['"http://localhost:3000"', "[1]", '[["http://localhost:3000"]]', "{}"])
def test_environment_variable_of_the_wrong_json_type_is_refused(monkeypatch, raw):
    monkeypatch.setenv("CORS_ALLOWED_ORIGINS", raw)
    with pytest.raises(ValidationError):
        Settings(_env_file=None)


@pytest.mark.parametrize(
    "origin",
    [
        WEB,
        "http://127.0.0.1:3000",
        "http://[::1]:3000",
        "http://web.localhost:3000",
        "https://app.example.edu",
        "https://app.example.edu:8443",
        "https://xn--bcher-kva.example",
    ],
)
def test_development_accepts_exact_origins(origin):
    assert settings([origin]).cors_allowed_origins == [origin]


@pytest.mark.parametrize(
    "origin",
    [
        "*",
        "null",
        "NULL",
        "http://*.example.edu",
        "https://*",
        "https://app.*.edu",
        "",
        " http://localhost:3000",
        "http://localhost:3000 ",
        "http://localhost:3000/",
        "http://localhost:3000/app",
        "http://localhost:3000?x=1",
        "http://localhost:3000#x",
        "http://user@localhost:3000",
        "http://user:pass@localhost:3000",
        "HTTP://localhost:3000",
        "http://LOCALHOST:3000",
        "localhost:3000",
        "//localhost:3000",
        "http://",
        "http://:3000",
        "ftp://example.edu",
        "file:///etc/passwd",
        "chrome-extension://abcdef",
        "ws://localhost:3000",
        "http://localhost:0",
        "http://localhost:03000",
        "http://localhost:65536",
        "http://localhost:",
        "http://example.edu:80",
        "https://example.edu:443",
        "http://[0:0:0:0:0:0:0:1]:3000",
        "http://[::g]:3000",
        "http://::1:3000",
        "http://999.1.1.1",
        "http://127.0.0.01:3000",
        "http://-bad.example.edu",
        "http://bad-.example.edu",
        "http://a..example.edu",
        "http://example.edu.",
        "http://exa_mple.edu",
        "https://exämple.edu",
        "http://localhost:3000\n",
        "http://localhost:3000,http://127.0.0.1:3000",
    ],
)
def test_malformed_wildcard_and_ambiguous_origins_are_refused(origin):
    with pytest.raises(ValidationError, match=r"CORS_ALLOWED_ORIGINS\[0\]"):
        settings([origin])


def test_refusal_names_the_entry_without_echoing_it():
    with pytest.raises(ValidationError) as caught:
        settings([WEB, "https://private-host.example.edu/"])
    message = str(caught.value)
    assert "CORS_ALLOWED_ORIGINS[1]" in message
    assert "private-host" not in message


def test_duplicate_origins_are_refused():
    with pytest.raises(ValidationError, match=r"CORS_ALLOWED_ORIGINS\[1\] is listed twice"):
        settings([WEB, WEB])


@pytest.mark.parametrize("environment", ["production", "staging", "Development", "prod", ""])
@pytest.mark.parametrize(
    "origin",
    [
        "http://app.example.edu",
        "https://localhost:3000",
        "https://web.localhost",
        "https://127.0.0.1",
        "https://127.8.9.10:8443",
        "https://[::1]:3000",
    ],
)
def test_outside_development_only_https_non_loopback_origins_are_accepted(environment, origin):
    with pytest.raises(ValidationError, match=r"CORS_ALLOWED_ORIGINS\[0\] must"):
        settings([origin], environment)


@pytest.mark.parametrize("environment", ["production", "staging"])
def test_outside_development_https_origins_are_accepted(environment):
    origins = ["https://app.example.edu", "https://staging.example.edu:8443"]
    assert settings(origins, environment).cors_allowed_origins == origins


def test_test_environment_counts_as_local():
    assert settings([WEB], "test").cors_allowed_origins == [WEB]


# --- no allowlist: same-origin only ----------------------------------------------------------------


def test_without_allowlist_no_cors_header_is_sent():
    test_client = client([])
    response = test_client.get("/health", headers={"Origin": WEB})
    assert response.status_code == 200
    assert cors_headers(response) == {}
    assert "vary" not in response.headers
    refused = preflight(test_client, WEB, "PUT", "authorization")
    assert refused.status_code == 405  # no CORS layer: the router answers the OPTIONS request
    assert cors_headers(refused) == {}


# --- simple and actual requests --------------------------------------------------------------------


def test_allowed_origin_gets_exact_origin_and_exposed_headers():
    response = client().get("/health", headers={"Origin": WEB})
    assert response.status_code == 200
    assert response.headers["access-control-allow-origin"] == WEB
    assert response.headers["access-control-expose-headers"] == ", ".join(EXPOSED_HEADERS)
    assert "access-control-allow-credentials" not in response.headers
    assert response.headers.get_list("vary") == ["Origin"]


def test_second_allowed_origin_is_echoed_exactly():
    other = "http://127.0.0.1:3000"
    response = client([WEB, other]).get("/health", headers={"Origin": other})
    assert response.headers["access-control-allow-origin"] == other


@pytest.mark.parametrize(
    "origin",
    [
        "https://evil.example",
        "null",
        "http://localhost:3001",
        "https://localhost:3000",
        "http://127.0.0.1:3000",
        "http://sub.localhost:3000",
        "http://localhost:3000/",
        "HTTP://LOCALHOST:3000",
        "http://localhost:3000.evil.example",
        "*",
    ],
)
def test_disallowed_origin_gets_no_cors_header_but_the_same_answer(origin):
    test_client = client()
    response = test_client.get("/health", headers={"Origin": origin})
    assert response.status_code == 200
    assert response.json() == test_client.get("/health").json()
    assert cors_headers(response) == {}
    assert response.headers.get_list("vary") == ["Origin"]


def test_request_without_origin_gets_no_cors_header_and_varies_on_origin():
    response = client().get("/health")
    assert response.status_code == 200
    assert cors_headers(response) == {}
    assert response.headers.get_list("vary") == ["Origin"]


def test_cookies_never_enable_credentials():
    test_client = client()
    test_client.cookies.set("session", "x")
    response = test_client.get("/health", headers={"Origin": WEB})
    assert response.headers["access-control-allow-origin"] == WEB
    assert "access-control-allow-credentials" not in response.headers
    refused = client().get("/health", headers={"Origin": "https://evil.example", "Cookie": "session=x"})
    assert cors_headers(refused) == {}


# --- preflight -------------------------------------------------------------------------------------


@pytest.mark.parametrize("method", ALLOWED_METHODS)
def test_preflight_allows_bearer_json_requests(method):
    response = preflight(client(), WEB, method, "authorization, content-type, idempotency-key, if-match")
    assert response.status_code == 200
    assert response.headers["access-control-allow-origin"] == WEB
    assert response.headers["access-control-allow-methods"] == ", ".join(ALLOWED_METHODS)
    allowed = {name.strip().lower() for name in response.headers["access-control-allow-headers"].split(",")}
    assert {name.lower() for name in ALLOWED_HEADERS} <= allowed
    assert response.headers["access-control-max-age"] == "600"
    assert "access-control-allow-credentials" not in response.headers
    assert response.headers.get_list("vary") == ["Origin"]


@pytest.mark.parametrize("method", ["TRACE", "CONNECT", "PROPFIND", "OPTIONS", "put"])
def test_preflight_refuses_unsupported_methods(method):
    response = preflight(client(), WEB, method)
    assert response.status_code == 400
    assert "method" in response.text


@pytest.mark.parametrize("header", ["x-api-key", "x-requested-with", "authorization, x-custom", "cookie"])
def test_preflight_refuses_unsupported_request_headers(header):
    response = preflight(client(), WEB, "POST", header)
    assert response.status_code == 400
    assert "headers" in response.text


@pytest.mark.parametrize("origin", ["https://evil.example", "null", "http://localhost:3001", "http://localhost:3000/"])
def test_preflight_from_disallowed_origin_gets_no_cors_header(origin):
    response = preflight(client(), origin, "PUT", "authorization, content-type")
    assert response.status_code == 400
    assert cors_headers(response) == {}
    assert response.headers.get_list("vary") == ["Origin"]


def test_preflight_for_private_network_access_is_refused():
    response = client().options(PROTECTED, headers={
        "Origin": WEB, "Access-Control-Request-Method": "GET", "Access-Control-Request-Private-Network": "true",
    })
    assert response.status_code == 400
    assert "access-control-allow-private-network" not in response.headers


# --- authentication and errors are unchanged -------------------------------------------------------


@pytest.mark.parametrize("origin", [None, WEB, "https://evil.example"])
@pytest.mark.parametrize("authorization", [None, "Bearer not-a-real-token"])
def test_cors_never_changes_authentication(origin, authorization):
    headers = {}
    if origin is not None:
        headers["Origin"] = origin
    if authorization is not None:
        headers["Authorization"] = authorization
    response = client().get(PROTECTED, headers=headers)
    assert response.status_code == 401
    assert response.json()["code"] == "not_authenticated"
    assert response.headers["www-authenticate"] == "Bearer"
    if origin == WEB:
        assert response.headers["access-control-allow-origin"] == WEB  # the browser may read the error
    else:
        assert cors_headers(response) == {}


def test_error_responses_carry_cors_headers_for_allowed_origins():
    response = client().get("/api/v1/does-not-exist", headers={"Origin": WEB})
    assert response.status_code == 404
    assert response.headers["access-control-allow-origin"] == WEB
    assert response.headers.get_list("vary") == ["Origin"]


# --- exposed headers match what the API emits ------------------------------------------------------

# Response headers in the published contract that are deliberately NOT exposed: CORS-safelisted
# (always readable), browser/proxy directives, and the 401 challenge (the JSON error body says the same).
NOT_EXPOSED = {
    "Cache-Control", "Content-Length", "Pragma",
    "Content-Security-Policy", "Referrer-Policy", "X-Content-Type-Options", "X-Accel-Buffering",
    "WWW-Authenticate",
}


def test_exposed_headers_are_exactly_the_contract_application_headers():
    # The schema the published contract is checked against in CI (scripts/export_openapi.py).
    contract = create_app().openapi()
    emitted: set[str] = set()
    for path in contract["paths"].values():
        for operation in path.values():
            for response in operation.get("responses", {}).values():
                emitted |= set(response.get("headers", {}))
    for response in contract.get("components", {}).get("responses", {}).values():
        emitted |= set(response.get("headers", {}))
    assert len(set(EXPOSED_HEADERS)) == len(EXPOSED_HEADERS)
    assert set(EXPOSED_HEADERS) == emitted - NOT_EXPOSED
