import pytest
from fastapi.testclient import TestClient
from main import app

client = TestClient(app)

def test_generate_endpoint_validation():
    response = client.post("/api/generate", json={"prompt": "test"})
    # Should fail due to missing required 'ddl_schema' field
    assert response.status_code == 422 
