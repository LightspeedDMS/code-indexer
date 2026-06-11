/* pathological.cpp — deeply nested templates, type traits, heavy generics,
   edge-case valid C++ that stresses the parser */

#include <type_traits>
#include <tuple>
#include <utility>
#include <string>

// ---------------------------------------------------------------------------
// Deeply nested template metaprogramming
// ---------------------------------------------------------------------------

// Integer list as variadic template pack
template <int... Ns>
struct IntList {};

// Prepend an integer to an IntList
template <int N, typename List>
struct Prepend;

template <int N, int... Ns>
struct Prepend<N, IntList<Ns...>> {
    using type = IntList<N, Ns...>;
};

// Generate IntList<0, 1, ..., N-1>
template <int N, int Acc = 0>
struct MakeRange {
    using type = typename Prepend<
        Acc,
        typename MakeRange<N, Acc + 1>::type
    >::type;
};

template <int N>
struct MakeRange<N, N> {
    using type = IntList<>;
};

// Compile-time sum of an IntList
template <typename List>
struct Sum;

template <>
struct Sum<IntList<>> {
    static constexpr int value = 0;
};

template <int Head, int... Tail>
struct Sum<IntList<Head, Tail...>> {
    static constexpr int value = Head + Sum<IntList<Tail...>>::value;
};

// ---------------------------------------------------------------------------
// Type traits chains: deeply nested conditional types
// ---------------------------------------------------------------------------

template <typename T>
struct DeepDecay {
    using type = typename std::remove_cv<
        typename std::remove_reference<
            typename std::decay<T>::type
        >::type
    >::type;
};

// Conditional type selection 4 levels deep
template <typename A, typename B, typename C, typename D>
struct SelectFirst {
    using type = typename std::conditional<
        std::is_integral<A>::value,
        A,
        typename std::conditional<
            std::is_floating_point<B>::value,
            B,
            typename std::conditional<
                std::is_pointer<C>::value,
                C,
                D
            >::type
        >::type
    >::type;
};

// ---------------------------------------------------------------------------
// Tuple manipulation with index_sequence
// ---------------------------------------------------------------------------

template <typename Tuple, std::size_t... Is>
auto tuple_tail_impl(Tuple &&t, std::index_sequence<Is...>)
    -> decltype(std::make_tuple(std::get<Is + 1>(std::forward<Tuple>(t))...)) {
    return std::make_tuple(std::get<Is + 1>(std::forward<Tuple>(t))...);
}

template <typename Head, typename... Tail>
std::tuple<Tail...> tuple_tail(std::tuple<Head, Tail...> &&t) {
    return tuple_tail_impl(
        std::move(t),
        std::make_index_sequence<sizeof...(Tail)>{}
    );
}

// ---------------------------------------------------------------------------
// Nested lambda captures and recursive lambda via std::function
// ---------------------------------------------------------------------------

#include <functional>

auto make_counter(int start) {
    return [start]() mutable {
        return [&start]() mutable {
            return start++;
        };
    }();
}

// ---------------------------------------------------------------------------
// Deeply nested class hierarchy
// ---------------------------------------------------------------------------

template <int Level>
struct Node : public Node<Level - 1> {
    int level = Level;
    virtual int depth() const { return Level; }
    virtual ~Node() = default;
};

template <>
struct Node<0> {
    int level = 0;
    virtual int depth() const { return 0; }
    virtual ~Node() = default;
};

// ---------------------------------------------------------------------------
// Variadic template recursion (print tuple elements)
// ---------------------------------------------------------------------------

template <std::size_t I = 0, typename... Ts>
typename std::enable_if<I == sizeof...(Ts)>::type
print_tuple(const std::tuple<Ts...> &) {}

template <std::size_t I = 0, typename... Ts>
typename std::enable_if<I < sizeof...(Ts)>::type
print_tuple(const std::tuple<Ts...> &t) {
    (void)std::get<I>(t);
    print_tuple<I + 1>(t);
}

// ---------------------------------------------------------------------------
// Main: instantiate the heavy templates to verify no parser failure
// ---------------------------------------------------------------------------

int main() {
    // Compile-time assertions
    using R5 = MakeRange<5>::type;
    static_assert(Sum<R5>::value == 10, "sum of 0..4 == 10");

    using Selected = SelectFirst<int, double, int *, std::string>::type;
    static_assert(std::is_same<Selected, int>::value, "int is integral");

    // Tuple tail
    auto t = std::make_tuple(1, 2.0, std::string("three"));
    auto tail = tuple_tail(std::move(t));
    (void)tail;

    // Nested lambda counter
    auto counter = make_counter(0);
    (void)counter();
    (void)counter();

    // Deep node hierarchy
    Node<5> n5;
    (void)n5.depth();

    // Variadic tuple print
    auto tup = std::make_tuple(42, 3.14, std::string("hi"));
    print_tuple(tup);

    return 0;
}
