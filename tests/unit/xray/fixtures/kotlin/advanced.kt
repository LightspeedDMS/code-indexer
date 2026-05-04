package com.example.kotlin.advanced

import kotlinx.coroutines.*
import kotlinx.coroutines.channels.*
import kotlinx.coroutines.flow.*

// Sealed interface with exhaustive when
sealed interface Command {
    data class Create(val name: String) : Command
    data class Update(val id: Long, val name: String) : Command
    data class Delete(val id: Long) : Command
    object Refresh : Command
}

// Type aliases
typealias CommandHandler<T> = suspend (Command) -> T
typealias EventListener = (String) -> Unit

// Generic class with variance
class EventBus<out T : Any>(private val capacity: Int = Channel.BUFFERED) {
    private val _channel = Channel<T>(capacity)
    val events: Flow<T> = _channel.receiveAsFlow()

    suspend fun emit(event: T) = _channel.send(event)
    fun close() = _channel.close()
}

// Context receivers (simulating with extension)
context(CoroutineScope)
fun <T> Flow<T>.collectWith(block: suspend (T) -> Unit): Job =
    launch { collect(block) }

// Delegation
interface Logger {
    fun log(message: String)
    fun error(message: String, cause: Throwable? = null)
}

class ConsoleLogger : Logger {
    override fun log(message: String) = println("[LOG] $message")
    override fun error(message: String, cause: Throwable?) {
        println("[ERR] $message${cause?.let { ": ${it.message}" } ?: ""}")
    }
}

class ServiceWithLogging(logger: Logger) : Logger by logger {
    fun process(input: String): String {
        log("Processing: $input")
        return input.reversed()
    }
}

// Destructuring and component functions
data class Triple<A, B, C>(val first: A, val second: B, val third: C) {
    operator fun component1() = first
    operator fun component2() = second
    operator fun component3() = third
}

// Operator overloading
data class Vector2D(val x: Double, val y: Double) {
    operator fun plus(other: Vector2D) = Vector2D(x + other.x, y + other.y)
    operator fun minus(other: Vector2D) = Vector2D(x - other.x, y - other.y)
    operator fun times(scalar: Double) = Vector2D(x * scalar, y * scalar)
    operator fun unaryMinus() = Vector2D(-x, -y)
    val magnitude: Double get() = Math.sqrt(x * x + y * y)
    fun normalize(): Vector2D {
        val m = magnitude
        return if (m == 0.0) Vector2D(0.0, 0.0) else Vector2D(x / m, y / m)
    }
}

// Coroutine channel producer
fun CoroutineScope.produceNumbers(from: Int, to: Int): ReceiveChannel<Int> = produce {
    for (i in from..to) {
        delay(10)
        send(i)
    }
}

// Flow operators chaining
fun processCommands(commands: Flow<Command>): Flow<String> = commands
    .filter { it !is Command.Refresh }
    .map { cmd ->
        when (cmd) {
            is Command.Create -> "Creating: ${cmd.name}"
            is Command.Update -> "Updating ${cmd.id}: ${cmd.name}"
            is Command.Delete -> "Deleting: ${cmd.id}"
            Command.Refresh -> "Refresh"
        }
    }
    .onEach { result -> println("Processed: $result") }
    .catch { e -> emit("Error: ${e.message}") }
    .flowOn(Dispatchers.Default)

// Suspend function with structured concurrency
suspend fun parallelFetch(ids: List<Long>): List<String> = coroutineScope {
    ids.map { id ->
        async(Dispatchers.IO) {
            "result_$id"
        }
    }.awaitAll()
}

// Inline reified function
inline fun <reified T> Flow<Any>.filterIsInstance(): Flow<T> =
    filter { it is T }.map { it as T }

// DSL builder pattern
class QueryBuilder {
    private val conditions = mutableListOf<String>()
    private var limit: Int = 100
    private var offset: Int = 0

    fun where(condition: String) = apply { conditions.add(condition) }
    fun limit(n: Int) = apply { limit = n }
    fun offset(n: Int) = apply { offset = n }

    fun build(): String {
        val where = if (conditions.isEmpty()) "" else " WHERE ${conditions.joinToString(" AND ")}"
        return "SELECT *$where LIMIT $limit OFFSET $offset"
    }
}

fun query(block: QueryBuilder.() -> Unit): String = QueryBuilder().apply(block).build()

// Usage of DSL
val exampleQuery = query {
    where("active = true")
    where("role = 'ADMIN'")
    limit(50)
    offset(0)
}
