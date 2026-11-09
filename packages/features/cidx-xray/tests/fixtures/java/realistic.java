package com.example.api.controller;

import com.example.api.dto.UserCreateRequest;
import com.example.api.dto.UserResponse;
import com.example.api.dto.UserUpdateRequest;
import com.example.api.exception.ResourceNotFoundException;
import com.example.api.exception.ValidationException;
import com.example.api.model.User;
import com.example.api.service.UserService;
import com.example.api.service.EmailService;
import com.example.api.security.JwtTokenProvider;
import jakarta.validation.constraints.NotNull;
import org.slf4j.Logger;
import org.slf4j.LoggerFactory;
import org.springframework.beans.factory.annotation.Autowired;
import org.springframework.data.domain.Page;
import org.springframework.data.domain.Pageable;
import org.springframework.data.web.PageableDefault;
import org.springframework.http.HttpHeaders;
import org.springframework.http.HttpStatus;
import org.springframework.http.MediaType;
import org.springframework.http.ResponseEntity;
import org.springframework.security.access.prepost.PreAuthorize;
import org.springframework.security.core.Authentication;
import org.springframework.validation.annotation.Validated;
import org.springframework.web.bind.annotation.*;
import org.springframework.web.multipart.MultipartFile;

import java.net.URI;
import java.time.Instant;
import java.util.*;
import java.util.stream.Collectors;

/**
 * REST controller for user management endpoints.
 * Handles CRUD operations and account lifecycle management.
 */
@RestController
@RequestMapping("/api/v1/users")
@CrossOrigin(origins = "*", maxAge = 3600)
@Validated
public class UserController {

    private static final Logger log = LoggerFactory.getLogger(UserController.class);
    private static final int MAX_EXPORT_SIZE = 10_000;
    private static final Set<String> ALLOWED_IMAGE_TYPES = Set.of("image/jpeg", "image/png", "image/webp");

    private final UserService userService;
    private final EmailService emailService;
    private final JwtTokenProvider tokenProvider;

    @Autowired
    public UserController(UserService userService,
                          EmailService emailService,
                          JwtTokenProvider tokenProvider) {
        this.userService = userService;
        this.emailService = emailService;
        this.tokenProvider = tokenProvider;
    }

    /**
     * List all users with pagination support.
     */
    @GetMapping(produces = MediaType.APPLICATION_JSON_VALUE)
    @PreAuthorize("hasRole('ADMIN')")
    public ResponseEntity<Page<UserResponse>> listUsers(
            @PageableDefault(size = 20, sort = "createdAt") Pageable pageable,
            @RequestParam(required = false) String search,
            @RequestParam(required = false) String role,
            Authentication authentication) {

        log.info("User {} listing users with search={} role={}", authentication.getName(), search, role);

        Page<User> users;
        if (search != null && !search.isBlank()) {
            users = userService.searchUsers(search, role, pageable);
        } else if (role != null) {
            users = userService.findByRole(role, pageable);
        } else {
            users = userService.findAll(pageable);
        }

        Page<UserResponse> response = users.map(this::toResponse);
        return ResponseEntity.ok()
                .header(HttpHeaders.CACHE_CONTROL, "no-cache, no-store")
                .body(response);
    }

    /**
     * Retrieve a single user by ID.
     */
    @GetMapping("/{id}")
    @PreAuthorize("hasRole('ADMIN') or @userSecurity.isCurrentUser(#id, authentication)")
    public ResponseEntity<UserResponse> getUser(
            @PathVariable @NotNull Long id,
            Authentication authentication) {

        User user = userService.findById(id)
                .orElseThrow(() -> new ResourceNotFoundException("User", "id", id));

        return ResponseEntity.ok(toResponse(user));
    }

    /**
     * Create a new user account.
     */
    @PostMapping(consumes = MediaType.APPLICATION_JSON_VALUE,
                 produces = MediaType.APPLICATION_JSON_VALUE)
    public ResponseEntity<UserResponse> createUser(@RequestBody UserCreateRequest request) {

        log.info("Creating new user with email: {}", request.getEmail());

        if (userService.existsByEmail(request.getEmail())) {
            throw new ValidationException("Email already registered: " + request.getEmail());
        }

        User user = userService.createUser(request);

        emailService.sendWelcomeEmail(user.getEmail(), user.getFirstName())
                .exceptionally(ex -> {
                    log.warn("Failed to send welcome email to {}: {}", user.getEmail(), ex.getMessage());
                    return null;
                });

        URI location = URI.create("/api/v1/users/" + user.getId());
        return ResponseEntity.created(location).body(toResponse(user));
    }

    /**
     * Update an existing user.
     */
    @PutMapping("/{id}")
    @PreAuthorize("hasRole('ADMIN') or @userSecurity.isCurrentUser(#id, authentication)")
    public ResponseEntity<UserResponse> updateUser(
            @PathVariable Long id,
            @RequestBody UserUpdateRequest request,
            Authentication authentication) {

        User existing = userService.findById(id)
                .orElseThrow(() -> new ResourceNotFoundException("User", "id", id));

        if (request.getEmail() != null
                && !request.getEmail().equals(existing.getEmail())
                && userService.existsByEmail(request.getEmail())) {
            throw new ValidationException("Email already in use: " + request.getEmail());
        }

        User updated = userService.updateUser(existing, request);
        log.info("User {} updated by {}", id, authentication.getName());

        return ResponseEntity.ok(toResponse(updated));
    }

    /**
     * Partially update a user (PATCH semantics).
     */
    @PatchMapping("/{id}")
    @PreAuthorize("hasRole('ADMIN') or @userSecurity.isCurrentUser(#id, authentication)")
    public ResponseEntity<UserResponse> patchUser(
            @PathVariable Long id,
            @RequestBody Map<String, Object> fields,
            Authentication authentication) {

        User existing = userService.findById(id)
                .orElseThrow(() -> new ResourceNotFoundException("User", "id", id));

        validatePatchFields(fields);
        User patched = userService.patchUser(existing, fields);
        log.info("User {} patched by {}", id, authentication.getName());

        return ResponseEntity.ok(toResponse(patched));
    }

    /**
     * Delete a user account.
     */
    @DeleteMapping("/{id}")
    @PreAuthorize("hasRole('ADMIN')")
    public ResponseEntity<Void> deleteUser(
            @PathVariable Long id,
            Authentication authentication) {

        User user = userService.findById(id)
                .orElseThrow(() -> new ResourceNotFoundException("User", "id", id));

        userService.softDelete(user);
        log.info("User {} soft-deleted by {}", id, authentication.getName());

        return ResponseEntity.noContent().build();
    }

    /**
     * Upload user avatar.
     */
    @PostMapping(value = "/{id}/avatar",
                 consumes = MediaType.MULTIPART_FORM_DATA_VALUE)
    @PreAuthorize("hasRole('ADMIN') or @userSecurity.isCurrentUser(#id, authentication)")
    public ResponseEntity<Map<String, String>> uploadAvatar(
            @PathVariable Long id,
            @RequestParam("file") MultipartFile file) {

        if (file.isEmpty()) {
            return ResponseEntity.badRequest()
                    .body(Map.of("error", "File must not be empty"));
        }

        String contentType = file.getContentType();
        if (contentType == null || !ALLOWED_IMAGE_TYPES.contains(contentType)) {
            return ResponseEntity.status(HttpStatus.UNSUPPORTED_MEDIA_TYPE)
                    .body(Map.of("error", "Only JPEG, PNG, or WebP images are accepted"));
        }

        if (file.getSize() > 5 * 1024 * 1024) {
            return ResponseEntity.status(HttpStatus.PAYLOAD_TOO_LARGE)
                    .body(Map.of("error", "Avatar must be smaller than 5 MB"));
        }

        String avatarUrl = userService.storeAvatar(id, file);
        return ResponseEntity.ok(Map.of("url", avatarUrl));
    }

    /**
     * Reset user password (admin-initiated).
     */
    @PostMapping("/{id}/reset-password")
    @PreAuthorize("hasRole('ADMIN')")
    public ResponseEntity<Void> resetPassword(
            @PathVariable Long id,
            Authentication authentication) {

        User user = userService.findById(id)
                .orElseThrow(() -> new ResourceNotFoundException("User", "id", id));

        String tempPassword = userService.resetPassword(user);
        emailService.sendPasswordReset(user.getEmail(), tempPassword);

        log.info("Password reset for user {} by {}", id, authentication.getName());
        return ResponseEntity.noContent().build();
    }

    /**
     * Bulk disable user accounts.
     */
    @PostMapping("/bulk/disable")
    @PreAuthorize("hasRole('SUPER_ADMIN')")
    public ResponseEntity<Map<String, Object>> bulkDisable(
            @RequestBody List<Long> userIds,
            Authentication authentication) {

        if (userIds.isEmpty()) {
            return ResponseEntity.badRequest()
                    .body(Map.of("error", "userIds must not be empty"));
        }

        if (userIds.size() > 500) {
            return ResponseEntity.status(HttpStatus.PAYLOAD_TOO_LARGE)
                    .body(Map.of("error", "Cannot disable more than 500 users at once"));
        }

        List<Long> failed = new ArrayList<>();
        List<Long> succeeded = new ArrayList<>();

        userIds.forEach(uid -> {
            try {
                userService.disableUser(uid);
                succeeded.add(uid);
            } catch (Exception e) {
                log.warn("Failed to disable user {}: {}", uid, e.getMessage());
                failed.add(uid);
            }
        });

        Map<String, Object> result = new LinkedHashMap<>();
        result.put("succeeded", succeeded);
        result.put("failed", failed);
        result.put("successCount", succeeded.size());
        result.put("failCount", failed.size());
        result.put("timestamp", Instant.now().toString());
        result.put("requestedBy", authentication.getName());

        return ResponseEntity.ok(result);
    }

    /**
     * Bulk enable user accounts.
     */
    @PostMapping("/bulk/enable")
    @PreAuthorize("hasRole('SUPER_ADMIN')")
    public ResponseEntity<Map<String, Object>> bulkEnable(
            @RequestBody List<Long> userIds,
            Authentication authentication) {

        if (userIds.isEmpty()) {
            return ResponseEntity.badRequest()
                    .body(Map.of("error", "userIds must not be empty"));
        }

        List<Long> failed = new ArrayList<>();
        List<Long> succeeded = new ArrayList<>();

        userIds.forEach(uid -> {
            try {
                userService.enableUser(uid);
                succeeded.add(uid);
            } catch (ResourceNotFoundException e) {
                log.warn("User {} not found during bulk enable", uid);
                failed.add(uid);
            } catch (Exception e) {
                log.error("Unexpected error enabling user {}: {}", uid, e.getMessage(), e);
                failed.add(uid);
            }
        });

        return ResponseEntity.ok(Map.of(
                "succeeded", succeeded,
                "failed", failed,
                "timestamp", Instant.now().toString()));
    }

    /**
     * Export users as CSV.
     */
    @GetMapping(value = "/export", produces = "text/csv")
    @PreAuthorize("hasRole('ADMIN')")
    public ResponseEntity<String> exportUsers(
            @RequestParam(required = false) String role,
            @RequestParam(defaultValue = "false") boolean includeDisabled) {

        List<User> users;
        if (role != null && includeDisabled) {
            users = userService.findByRoleIncludingDisabled(role);
        } else if (role != null) {
            users = userService.findByRoleUnpaged(role);
        } else if (includeDisabled) {
            users = userService.findAllIncludingDisabled();
        } else {
            users = userService.findAllUnpaged();
        }

        if (users.size() > MAX_EXPORT_SIZE) {
            users = users.subList(0, MAX_EXPORT_SIZE);
            log.warn("Export truncated to {} records", MAX_EXPORT_SIZE);
        }

        String csv = buildCsv(users);

        return ResponseEntity.ok()
                .header(HttpHeaders.CONTENT_DISPOSITION,
                        "attachment; filename=\"users-export.csv\"")
                .header(HttpHeaders.CACHE_CONTROL, "no-store")
                .body(csv);
    }

    /**
     * Get current user's own profile.
     */
    @GetMapping("/me")
    public ResponseEntity<UserResponse> getCurrentUser(Authentication authentication) {
        String username = authentication.getName();
        User user = userService.findByUsername(username)
                .orElseThrow(() -> new ResourceNotFoundException("User", "username", username));
        return ResponseEntity.ok(toResponse(user));
    }

    /**
     * Search users by various criteria.
     */
    @GetMapping("/search")
    @PreAuthorize("hasRole('ADMIN')")
    public ResponseEntity<List<UserResponse>> searchUsers(
            @RequestParam String q,
            @RequestParam(defaultValue = "10") int limit) {

        if (q.length() < 2) {
            return ResponseEntity.badRequest().build();
        }

        if (limit < 1 || limit > 100) {
            return ResponseEntity.badRequest().build();
        }

        List<UserResponse> results = userService.fullTextSearch(q, limit)
                .stream()
                .map(this::toResponse)
                .collect(Collectors.toList());

        return ResponseEntity.ok(results);
    }

    /**
     * Get user activity statistics.
     */
    @GetMapping("/{id}/stats")
    @PreAuthorize("hasRole('ADMIN') or @userSecurity.isCurrentUser(#id, authentication)")
    public ResponseEntity<Map<String, Object>> getUserStats(@PathVariable Long id) {
        User user = userService.findById(id)
                .orElseThrow(() -> new ResourceNotFoundException("User", "id", id));

        Map<String, Object> stats = new LinkedHashMap<>();
        stats.put("loginCount", userService.getLoginCount(id));
        stats.put("lastLogin", userService.getLastLogin(id).map(Object::toString).orElse(null));
        stats.put("accountAge", userService.getAccountAgeDays(id));
        stats.put("activeSessionCount", userService.getActiveSessionCount(id));
        stats.put("role", user.getRole().name());
        stats.put("enabled", user.isEnabled());
        stats.put("emailVerified", user.isEmailVerified());
        stats.put("twoFactorEnabled", user.isTwoFactorEnabled());

        return ResponseEntity.ok(stats);
    }

    /**
     * Verify email address via token.
     */
    @GetMapping("/verify-email")
    public ResponseEntity<Map<String, String>> verifyEmail(@RequestParam String token) {
        boolean verified = userService.verifyEmailToken(token);
        if (verified) {
            return ResponseEntity.ok(Map.of("status", "verified"));
        } else {
            return ResponseEntity.status(HttpStatus.BAD_REQUEST)
                    .body(Map.of("error", "Invalid or expired verification token"));
        }
    }

    /**
     * Resend email verification link.
     */
    @PostMapping("/{id}/resend-verification")
    @PreAuthorize("hasRole('ADMIN') or @userSecurity.isCurrentUser(#id, authentication)")
    public ResponseEntity<Void> resendVerification(@PathVariable Long id) {
        User user = userService.findById(id)
                .orElseThrow(() -> new ResourceNotFoundException("User", "id", id));

        if (user.isEmailVerified()) {
            return ResponseEntity.status(HttpStatus.CONFLICT).build();
        }

        String verificationToken = userService.generateVerificationToken(user);
        emailService.sendVerificationEmail(user.getEmail(), verificationToken);

        return ResponseEntity.noContent().build();
    }

    /**
     * Get audit log for a user.
     */
    @GetMapping("/{id}/audit")
    @PreAuthorize("hasRole('SUPER_ADMIN')")
    public ResponseEntity<List<Map<String, Object>>> getAuditLog(
            @PathVariable Long id,
            @RequestParam(defaultValue = "50") int limit,
            @RequestParam(required = false) String eventType) {

        userService.findById(id)
                .orElseThrow(() -> new ResourceNotFoundException("User", "id", id));

        List<Map<String, Object>> auditEntries = userService.getAuditLog(id, eventType, limit);
        return ResponseEntity.ok(auditEntries);
    }

    /**
     * Generate a new API token for the user.
     */
    @PostMapping("/{id}/api-tokens")
    @PreAuthorize("hasRole('ADMIN') or @userSecurity.isCurrentUser(#id, authentication)")
    public ResponseEntity<Map<String, String>> generateApiToken(
            @PathVariable Long id,
            @RequestBody Map<String, String> request,
            Authentication authentication) {

        User user = userService.findById(id)
                .orElseThrow(() -> new ResourceNotFoundException("User", "id", id));

        String tokenName = request.getOrDefault("name", "API Token " + Instant.now());
        String expiresIn = request.getOrDefault("expiresIn", "30d");

        String token = tokenProvider.generateApiToken(user, tokenName, expiresIn);
        userService.storeApiToken(id, tokenName, token);

        log.info("API token '{}' generated for user {} by {}", tokenName, id, authentication.getName());

        return ResponseEntity.status(HttpStatus.CREATED)
                .body(Map.of("token", token, "name", tokenName, "expiresIn", expiresIn));
    }

    /**
     * Revoke an API token.
     */
    @DeleteMapping("/{id}/api-tokens/{tokenId}")
    @PreAuthorize("hasRole('ADMIN') or @userSecurity.isCurrentUser(#id, authentication)")
    public ResponseEntity<Void> revokeApiToken(
            @PathVariable Long id,
            @PathVariable String tokenId,
            Authentication authentication) {

        userService.findById(id)
                .orElseThrow(() -> new ResourceNotFoundException("User", "id", id));

        boolean revoked = userService.revokeApiToken(id, tokenId);
        if (!revoked) {
            throw new ResourceNotFoundException("ApiToken", "id", tokenId);
        }

        log.info("API token {} revoked for user {} by {}", tokenId, id, authentication.getName());
        return ResponseEntity.noContent().build();
    }

    // -------------------------------------------------------------------------
    // Private helpers
    // -------------------------------------------------------------------------

    private UserResponse toResponse(User user) {
        UserResponse resp = new UserResponse();
        resp.setId(user.getId());
        resp.setUsername(user.getUsername());
        resp.setEmail(user.getEmail());
        resp.setFirstName(user.getFirstName());
        resp.setLastName(user.getLastName());
        resp.setRole(user.getRole().name());
        resp.setEnabled(user.isEnabled());
        resp.setEmailVerified(user.isEmailVerified());
        resp.setCreatedAt(user.getCreatedAt());
        resp.setUpdatedAt(user.getUpdatedAt());
        resp.setAvatarUrl(user.getAvatarUrl());
        return resp;
    }

    private String buildCsv(List<User> users) {
        StringBuilder sb = new StringBuilder();
        sb.append("id,username,email,firstName,lastName,role,enabled,emailVerified,createdAt\n");
        users.forEach(u -> sb.append(String.format("%d,%s,%s,%s,%s,%s,%b,%b,%s\n",
                u.getId(),
                escapeCsv(u.getUsername()),
                escapeCsv(u.getEmail()),
                escapeCsv(u.getFirstName()),
                escapeCsv(u.getLastName()),
                u.getRole().name(),
                u.isEnabled(),
                u.isEmailVerified(),
                u.getCreatedAt())));
        return sb.toString();
    }

    private String escapeCsv(String value) {
        if (value == null) return "";
        if (value.contains(",") || value.contains("\"") || value.contains("\n")) {
            return "\"" + value.replace("\"", "\"\"") + "\"";
        }
        return value;
    }

    private void validatePatchFields(Map<String, Object> fields) {
        Set<String> allowed = Set.of("firstName", "lastName", "email", "password", "role", "twoFactorEnabled");
        Set<String> unknown = new HashSet<>(fields.keySet());
        unknown.removeAll(allowed);
        if (!unknown.isEmpty()) {
            throw new ValidationException("Unknown fields in patch: " + unknown);
        }
    }
}
