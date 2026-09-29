import pytest
from .pipeline import format_error


class MockSDKErrorWithBody(Exception):
    def __init__(self, message: str, body: dict):
        super().__init__(message)
        self.body = body


class MockHTTPResponseError(Exception):
    def __init__(self, message: str, response_data: dict):
        super().__init__(message)
        class Response:
            def json(self):
                return response_data
        self.response = Response()


def test_format_error_with_structured_sdk_body():
    exc = MockSDKErrorWithBody(
        "Error code: 400 - {'error': {'message': \"Failed to generate JSON. Please adjust your prompt. See 'failed_generation' for more details.\", 'type': 'invalid_request_error', 'code': 'json_validate_failed', 'failed_generation': 'I need a bit more info.'}}",
        {
            "error": {
                "message": "Failed to generate JSON. Please adjust your prompt. See 'failed_generation' for more details.",
                "type": "invalid_request_error",
                "code": "json_validate_failed",
                "failed_generation": "I need a bit more info.",
            }
        }
    )
    result = format_error(exc)
    assert "Failed to generate JSON" in result
    assert "I need a bit more info." in result
    assert "See 'failed_generation' for more details." not in result


def test_format_error_with_stringified_python_dict():
    # Simulates when an upstream exception stringified the dict
    raw_str = (
        "Error code: 400 - {'error': {'message': 'Rate limit reached on free tier. Retry after 15s.', "
        "'type': 'rate_limit_error', 'code': 'rate_limit_exceeded'}}"
    )
    exc = Exception(raw_str)
    result = format_error(exc)
    assert result == "Rate limit reached on free tier. Retry after 15s."


def test_format_error_with_http_response_object():
    exc = MockHTTPResponseError("400 Bad Request", {"error": {"message": "Invalid query syntax"}})
    result = format_error(exc)
    assert result == "Invalid query syntax"


def test_format_error_generic_fallback():
    exc = TimeoutError("Connection to search provider timed out")
    result = format_error(exc)
    assert result == "Connection to search provider timed out"


def test_format_error_none():
    result = format_error(None)
    assert "unknown error" in result.lower()
