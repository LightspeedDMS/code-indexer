# Database Schema Guide

This document describes the database schema used by the application.

## User Table Schema

The users table stores all registered user accounts with their authentication credentials.

![Database Schema](../images/database-schema.png)

### Column Details

- **user_id**: Auto-incrementing primary key for unique user identification
- **username**: Unique login identifier, must be between 3-50 characters
- **email**: User's email address for notifications and password recovery
- **created_at**: Timestamp when the account was created
- **password_hash**: Bcrypt hashed password with salt

### Usage Examples

```sql
-- Find user by username
SELECT * FROM users WHERE username = 'johndoe';

-- Get users created in last 24 hours
SELECT * FROM users WHERE created_at > NOW() - INTERVAL '24 hours';
```

## Important Notes

The password_hash column uses bcrypt with a cost factor of 12. Never store plaintext passwords.
