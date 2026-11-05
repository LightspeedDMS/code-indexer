package handler

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"log"
	"net/http"
	"strconv"
	"sync"
	"time"
)

// User represents a system user
type User struct {
	ID        int64     `json:"id"`
	Name      string    `json:"name"`
	Email     string    `json:"email"`
	Role      string    `json:"role"`
	CreatedAt time.Time `json:"created_at"`
	Enabled   bool      `json:"enabled"`
}

// UserService defines the user business logic interface
type UserService interface {
	GetUser(ctx context.Context, id int64) (*User, error)
	ListUsers(ctx context.Context, page, size int) ([]*User, int64, error)
	CreateUser(ctx context.Context, req CreateUserRequest) (*User, error)
	UpdateUser(ctx context.Context, id int64, req UpdateUserRequest) (*User, error)
	DeleteUser(ctx context.Context, id int64) error
}

// AuditLogger logs audit events asynchronously
type AuditLogger interface {
	Log(event string, userID int64, actor string)
}

// CreateUserRequest holds data for user creation
type CreateUserRequest struct {
	Name  string `json:"name"`
	Email string `json:"email"`
	Role  string `json:"role"`
}

// UpdateUserRequest holds data for user update
type UpdateUserRequest struct {
	Name  *string `json:"name,omitempty"`
	Email *string `json:"email,omitempty"`
	Role  *string `json:"role,omitempty"`
}

// ErrorResponse is returned on API errors
type ErrorResponse struct {
	Code    int    `json:"code"`
	Message string `json:"message"`
}

// auditEvent bundles an audit message for async dispatch
type auditEvent struct {
	event  string
	userID int64
	actor  string
}

// UserHandler handles HTTP requests for user endpoints
type UserHandler struct {
	service    UserService
	audit      AuditLogger
	logger     *log.Logger
	mu         sync.RWMutex
	cache      map[int64]*User
	auditQueue chan auditEvent
	done       chan struct{}
}

// NewUserHandler creates a new UserHandler and starts background audit worker
func NewUserHandler(service UserService, audit AuditLogger, logger *log.Logger) *UserHandler {
	h := &UserHandler{
		service:    service,
		audit:      audit,
		logger:     logger,
		cache:      make(map[int64]*User),
		auditQueue: make(chan auditEvent, 256),
		done:       make(chan struct{}),
	}
	// Start background goroutine for async audit logging
	go h.auditWorker()
	return h
}

// auditWorker drains the audit queue in the background
func (h *UserHandler) auditWorker() {
	for {
		select {
		case ev := <-h.auditQueue:
			h.audit.Log(ev.event, ev.userID, ev.actor)
		case <-h.done:
			// Drain remaining events before stopping
			for {
				select {
				case ev := <-h.auditQueue:
					h.audit.Log(ev.event, ev.userID, ev.actor)
				default:
					return
				}
			}
		}
	}
}

// Shutdown stops the background audit worker
func (h *UserHandler) Shutdown() {
	close(h.done)
}

// enqueueAudit sends an audit event without blocking the request path
func (h *UserHandler) enqueueAudit(event string, userID int64, actor string) {
	select {
	case h.auditQueue <- auditEvent{event, userID, actor}:
	default:
		h.logger.Printf("WARN audit queue full, dropping event: %s", event)
	}
}

// RegisterRoutes registers the handler's routes on the given mux
func (h *UserHandler) RegisterRoutes(mux *http.ServeMux) {
	mux.HandleFunc("/api/users", h.handleUsers)
	mux.HandleFunc("/api/users/", h.handleUser)
}

func (h *UserHandler) handleUsers(w http.ResponseWriter, r *http.Request) {
	switch r.Method {
	case http.MethodGet:
		h.listUsers(w, r)
	case http.MethodPost:
		h.createUser(w, r)
	default:
		h.writeError(w, http.StatusMethodNotAllowed, "method not allowed")
	}
}

func (h *UserHandler) handleUser(w http.ResponseWriter, r *http.Request) {
	idStr := r.URL.Path[len("/api/users/"):]
	id, err := strconv.ParseInt(idStr, 10, 64)
	if err != nil {
		h.writeError(w, http.StatusBadRequest, "invalid user id")
		return
	}

	switch r.Method {
	case http.MethodGet:
		h.getUser(w, r, id)
	case http.MethodPut:
		h.updateUser(w, r, id)
	case http.MethodDelete:
		h.deleteUser(w, r, id)
	default:
		h.writeError(w, http.StatusMethodNotAllowed, "method not allowed")
	}
}

func (h *UserHandler) listUsers(w http.ResponseWriter, r *http.Request) {
	ctx := r.Context()
	page := parseQueryInt(r, "page", 0)
	size := parseQueryInt(r, "size", 20)

	if size > 100 {
		size = 100
	}

	users, total, err := h.service.ListUsers(ctx, page, size)
	if err != nil {
		h.logger.Printf("ERROR listing users: %v", err)
		h.writeError(w, http.StatusInternalServerError, "failed to list users")
		return
	}

	resp := map[string]interface{}{
		"items": users,
		"total": total,
		"page":  page,
		"size":  size,
	}
	h.writeJSON(w, http.StatusOK, resp)
}

func (h *UserHandler) getUser(w http.ResponseWriter, r *http.Request, id int64) {
	ctx := r.Context()

	h.mu.RLock()
	cached, ok := h.cache[id]
	h.mu.RUnlock()

	if ok {
		h.writeJSON(w, http.StatusOK, cached)
		return
	}

	user, err := h.service.GetUser(ctx, id)
	if err != nil {
		var notFound *NotFoundError
		if errors.As(err, &notFound) {
			h.writeError(w, http.StatusNotFound, notFound.Error())
			return
		}
		h.logger.Printf("ERROR getting user %d: %v", id, err)
		h.writeError(w, http.StatusInternalServerError, "failed to get user")
		return
	}

	h.mu.Lock()
	h.cache[id] = user
	h.mu.Unlock()

	h.writeJSON(w, http.StatusOK, user)
}

func (h *UserHandler) createUser(w http.ResponseWriter, r *http.Request) {
	ctx := r.Context()
	actor := r.Header.Get("X-User")

	var req CreateUserRequest
	if err := json.NewDecoder(r.Body).Decode(&req); err != nil {
		h.writeError(w, http.StatusBadRequest, "invalid request body")
		return
	}

	if req.Name == "" || req.Email == "" {
		h.writeError(w, http.StatusUnprocessableEntity, "name and email are required")
		return
	}

	user, err := h.service.CreateUser(ctx, req)
	if err != nil {
		var conflict *ConflictError
		if errors.As(err, &conflict) {
			h.writeError(w, http.StatusConflict, conflict.Error())
			return
		}
		h.logger.Printf("ERROR creating user: %v", err)
		h.writeError(w, http.StatusInternalServerError, "failed to create user")
		return
	}

	// Fire-and-forget audit log via goroutine-backed queue
	h.enqueueAudit("user.created", user.ID, actor)

	// Kick off a goroutine to warm the cache
	go func(u *User) {
		h.mu.Lock()
		h.cache[u.ID] = u
		h.mu.Unlock()
	}(user)

	w.Header().Set("Location", fmt.Sprintf("/api/users/%d", user.ID))
	h.writeJSON(w, http.StatusCreated, user)
}

func (h *UserHandler) updateUser(w http.ResponseWriter, r *http.Request, id int64) {
	ctx := r.Context()
	actor := r.Header.Get("X-User")

	var req UpdateUserRequest
	if err := json.NewDecoder(r.Body).Decode(&req); err != nil {
		h.writeError(w, http.StatusBadRequest, "invalid request body")
		return
	}

	user, err := h.service.UpdateUser(ctx, id, req)
	if err != nil {
		var notFound *NotFoundError
		if errors.As(err, &notFound) {
			h.writeError(w, http.StatusNotFound, notFound.Error())
			return
		}
		h.logger.Printf("ERROR updating user %d: %v", id, err)
		h.writeError(w, http.StatusInternalServerError, "failed to update user")
		return
	}

	h.enqueueAudit("user.updated", id, actor)

	h.mu.Lock()
	h.cache[id] = user
	h.mu.Unlock()

	h.writeJSON(w, http.StatusOK, user)
}

func (h *UserHandler) deleteUser(w http.ResponseWriter, r *http.Request, id int64) {
	ctx := r.Context()
	actor := r.Header.Get("X-User")

	if err := h.service.DeleteUser(ctx, id); err != nil {
		var notFound *NotFoundError
		if errors.As(err, &notFound) {
			h.writeError(w, http.StatusNotFound, notFound.Error())
			return
		}
		h.logger.Printf("ERROR deleting user %d: %v", id, err)
		h.writeError(w, http.StatusInternalServerError, "failed to delete user")
		return
	}

	h.enqueueAudit("user.deleted", id, actor)

	h.mu.Lock()
	delete(h.cache, id)
	h.mu.Unlock()

	w.WriteHeader(http.StatusNoContent)
}

// writeJSON encodes v as JSON and writes to w
func (h *UserHandler) writeJSON(w http.ResponseWriter, status int, v interface{}) {
	w.Header().Set("Content-Type", "application/json")
	w.WriteHeader(status)
	if err := json.NewEncoder(w).Encode(v); err != nil {
		h.logger.Printf("ERROR encoding JSON response: %v", err)
	}
}

// writeError writes an error response
func (h *UserHandler) writeError(w http.ResponseWriter, status int, message string) {
	h.writeJSON(w, status, ErrorResponse{Code: status, Message: message})
}

// NotFoundError indicates a resource was not found
type NotFoundError struct {
	Resource string
	ID       interface{}
}

func (e *NotFoundError) Error() string {
	return fmt.Sprintf("%s with id %v not found", e.Resource, e.ID)
}

// ConflictError indicates a resource conflict
type ConflictError struct {
	Message string
}

func (e *ConflictError) Error() string {
	return e.Message
}

// parseQueryInt parses an integer query parameter with a default value
func parseQueryInt(r *http.Request, key string, defaultVal int) int {
	s := r.URL.Query().Get(key)
	if s == "" {
		return defaultVal
	}
	v, err := strconv.Atoi(s)
	if err != nil {
		return defaultVal
	}
	return v
}
