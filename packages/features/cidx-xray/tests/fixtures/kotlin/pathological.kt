package com.example.kotlin.pathological

// Deeply nested lambda chains
val deeplyNested: (List<List<List<String>>>) -> String = { data ->
    data.flatMap { outer ->
        outer.flatMap { inner ->
            inner.filter { it.isNotBlank() }
                .map { s ->
                    s.trim().lowercase()
                        .replace(Regex("[^a-z0-9]"), "_")
                        .let { slug ->
                            if (slug.length > 20) slug.substring(0, 20) else slug
                        }
                }
        }
    }.distinct().sorted().joinToString(", ")
}

// Deeply nested when expressions
fun classify(x: Int): String = when {
    x < 0 -> when {
        x < -1000 -> when {
            x < -10000 -> "very deeply negative"
            else -> "deeply negative"
        }
        x < -100 -> "moderately negative"
        else -> "slightly negative"
    }
    x == 0 -> "zero"
    else -> when {
        x > 10000 -> when {
            x > 100000 -> "astronomically positive"
            else -> "very large positive"
        }
        x > 100 -> "large positive"
        else -> "small positive"
    }
}

// Many parameters function
fun buildRecord(
    id: Long, name: String, email: String, phone: String,
    address: String, city: String, state: String, zip: String,
    country: String, active: Boolean, priority: Int, score: Double,
    tags: List<String>, metadata: Map<String, String>
): Map<String, Any?> = mapOf(
    "id" to id, "name" to name, "email" to email, "phone" to phone,
    "address" to address, "city" to city, "state" to state, "zip" to zip,
    "country" to country, "active" to active, "priority" to priority,
    "score" to score, "tags" to tags, "metadata" to metadata
)

// Long single-line chained call (stress for line length)
fun processAll(items: List<String>): List<String> = items.filter { it.isNotBlank() }.map { it.trim().lowercase() }.flatMap { it.split(",") }.map { it.trim() }.filter { it.length >= 2 }.distinct().sorted().map { it.replaceFirstChar { c -> c.uppercaseChar() } }

// Nested scope functions
fun nested(input: String?): String {
    return input?.let { raw ->
        raw.trim().let { trimmed ->
            trimmed.takeIf { it.isNotEmpty() }?.let { nonEmpty ->
                nonEmpty.also { println("Processing: $it") }
                    .run { uppercase() }
                    .also { result -> println("Result: $result") }
            } ?: "empty"
        }
    } ?: "null"
}

// Generic function with multiple bounds
fun <T> List<T>.safeGet(index: Int, default: T): T =
    if (index in indices) get(index) else default

// Recursive function
tailrec fun factorial(n: Long, acc: Long = 1L): Long =
    if (n <= 1L) acc else factorial(n - 1L, n * acc)

// String interpolation with complex expressions
fun format(items: List<Pair<String, Int>>): String =
    "Items: ${items.size}, Total: ${items.sumOf { it.second }}, " +
    "Max: ${items.maxOfOrNull { it.second } ?: 0}, " +
    "Keys: ${items.map { it.first }.sorted().joinToString("|")}"

// Anonymous object
val comparator = object : Comparator<String> {
    override fun compare(a: String, b: String): Int =
        when {
            a.length != b.length -> a.length - b.length
            else -> a.compareTo(b)
        }
}
