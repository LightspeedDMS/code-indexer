package com.example.kotlin

import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.async
import kotlinx.coroutines.coroutineScope
import kotlinx.coroutines.flow.Flow
import kotlinx.coroutines.flow.filter
import kotlinx.coroutines.flow.map
import kotlinx.coroutines.flow.toList
import kotlinx.coroutines.withContext
import java.time.Instant

// Data classes
data class User(
    val id: Long,
    val name: String,
    val email: String,
    val role: UserRole,
    val createdAt: Instant = Instant.now(),
    val enabled: Boolean = true
)

data class UserCreateRequest(
    val name: String,
    val email: String,
    val role: UserRole = UserRole.VIEWER
)

enum class UserRole { ADMIN, EDITOR, VIEWER }

data class Page<T>(
    val items: List<T>,
    val total: Long,
    val page: Int,
    val size: Int
) {
    val totalPages: Int get() = if (size == 0) 0 else ((total + size - 1) / size).toInt()
    val hasNext: Boolean get() = page < totalPages - 1
    val hasPrev: Boolean get() = page > 0

    fun <R> map(transform: (T) -> R): Page<R> =
        Page(items.map(transform), total, page, size)
}

// Sealed interface for result types
sealed interface Result<out T> {
    data class Success<T>(val value: T) : Result<T>
    data class Failure(val error: Throwable) : Result<Nothing>
    object Loading : Result<Nothing>

    val isSuccess: Boolean get() = this is Success
    val isFailure: Boolean get() = this is Failure
}

// Extension functions
fun <T> Result<T>.getOrNull(): T? = (this as? Result.Success)?.value
fun <T> Result<T>.getOrThrow(): T = when (this) {
    is Result.Success -> value
    is Result.Failure -> throw error
    is Result.Loading -> throw IllegalStateException("Still loading")
}

fun String.toSlug(): String = this
    .trim()
    .lowercase()
    .replace(Regex("[^a-z0-9\\s-]"), "")
    .replace(Regex("\\s+"), "-")

fun User.toDisplayName(): String = "${name} <${email}>"

// Repository interface
interface UserRepository {
    suspend fun findById(id: Long): User?
    suspend fun findAll(page: Int, size: Int): Page<User>
    suspend fun findByEmail(email: String): User?
    suspend fun save(user: User): User
    suspend fun delete(id: Long): Boolean
    fun observeAll(): Flow<User>
}

// Service with coroutines
class UserService(private val repo: UserRepository) {

    companion object {
        private const val DEFAULT_PAGE_SIZE = 20
        private const val MAX_PAGE_SIZE = 100
    }

    suspend fun getUser(id: Long): Result<User> = withContext(Dispatchers.IO) {
        try {
            val user = repo.findById(id) ?: return@withContext Result.Failure(
                NoSuchElementException("User $id not found")
            )
            Result.Success(user)
        } catch (e: Exception) {
            Result.Failure(e)
        }
    }

    suspend fun createUser(request: UserCreateRequest): Result<User> {
        val existing = repo.findByEmail(request.email)
        if (existing != null) {
            return Result.Failure(IllegalArgumentException("Email already exists: ${request.email}"))
        }

        val user = User(
            id = System.currentTimeMillis(),
            name = request.name.trim(),
            email = request.email.lowercase().trim(),
            role = request.role
        )

        return try {
            Result.Success(repo.save(user))
        } catch (e: Exception) {
            Result.Failure(e)
        }
    }

    suspend fun listUsers(page: Int = 0, size: Int = DEFAULT_PAGE_SIZE): Result<Page<User>> {
        val effectiveSize = size.coerceIn(1, MAX_PAGE_SIZE)
        return try {
            Result.Success(repo.findAll(page, effectiveSize))
        } catch (e: Exception) {
            Result.Failure(e)
        }
    }

    suspend fun deleteUser(id: Long): Result<Unit> {
        val exists = repo.findById(id) != null
        if (!exists) {
            return Result.Failure(NoSuchElementException("User $id not found"))
        }
        return try {
            repo.delete(id)
            Result.Success(Unit)
        } catch (e: Exception) {
            Result.Failure(e)
        }
    }

    suspend fun bulkCreate(requests: List<UserCreateRequest>): Map<String, Result<User>> =
        coroutineScope {
            requests
                .map { req -> req.email to async { createUser(req) } }
                .associate { (email, deferred) -> email to deferred.await() }
        }

    fun observeActiveUsers(): Flow<User> = repo.observeAll()
        .filter { it.enabled }
        .map { user ->
            user.copy(name = user.name.trim())
        }

    suspend fun getUsersByRole(role: UserRole): List<User> {
        val allUsers = mutableListOf<User>()
        var page = 0
        while (true) {
            val result = repo.findAll(page, DEFAULT_PAGE_SIZE)
            allUsers.addAll(result.items.filter { it.role == role })
            if (!result.hasNext) break
            page++
        }
        return allUsers
    }
}

// Extension on service
suspend fun UserService.getUserOrThrow(id: Long): User =
    getUser(id).getOrThrow()

// Inline function with reified type
inline fun <reified T> Any?.cast(): T? = this as? T

// Scope function usage patterns
fun buildUserSummary(user: User): Map<String, Any> = buildMap {
    put("id", user.id)
    put("name", user.name)
    put("email", user.email)
    put("role", user.role.name)
    put("enabled", user.enabled)
    put("slug", user.name.toSlug())
    put("display", user.toDisplayName())
}

// Object (singleton)
object UserCache {
    private val cache = mutableMapOf<Long, User>()

    fun get(id: Long): User? = cache[id]
    fun put(user: User) { cache[user.id] = user }
    fun invalidate(id: Long) { cache.remove(id) }
    fun clear() { cache.clear() }
    val size: Int get() = cache.size
}
