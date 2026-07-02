# API Reference

Complete reference for the REST API authentication and authorization flow.

## Authentication Flow

The API uses JWT bearer tokens for authentication. Here's the complete flow:

![API Authentication Flow](../images/api-flow.jpg)

## Endpoints

### POST /api/auth/login

Authenticates a user and returns a JWT token.

**Request Body:**
```json
{
  "username": "string",
  "password": "string"
}
```

**Response:**
```json
{
  "token": "eyJhbGciOiJIUzI1NiIs...",
  "expires_in": 3600
}
```

### GET /api/protected/resource

Access a protected resource using the bearer token.

**Headers:**
```
Authorization: Bearer <jwt_token>
```

## Token Validation

The server validates the JWT token on every request to protected endpoints:

1. Extracts token from Authorization header
2. Verifies signature using secret key
3. Checks expiration timestamp
4. Validates claims and permissions

## Security Best Practices

- Store tokens securely (httpOnly cookies or secure storage)
- Use short expiration times (1 hour recommended)
- Implement token refresh mechanism
- Always use HTTPS in production
