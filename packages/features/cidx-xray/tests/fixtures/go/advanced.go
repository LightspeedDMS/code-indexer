package advanced

import (
	"context"
	"fmt"
	"sort"
	"sync"
)

// Ordered is a constraint for types that can be compared with < and >
type Ordered interface {
	~int | ~int8 | ~int16 | ~int32 | ~int64 |
		~uint | ~uint8 | ~uint16 | ~uint32 | ~uint64 | ~uintptr |
		~float32 | ~float64 | ~string
}

// Min returns the smaller of two ordered values (generics)
func Min[T Ordered](a, b T) T {
	if a < b {
		return a
	}
	return b
}

// Max returns the larger of two ordered values (generics)
func Max[T Ordered](a, b T) T {
	if a > b {
		return a
	}
	return b
}

// Map applies f to every element of s and returns the results (generics)
func Map[T, U any](s []T, f func(T) U) []U {
	result := make([]U, len(s))
	for i, v := range s {
		result[i] = f(v)
	}
	return result
}

// Filter returns elements of s for which predicate returns true (generics)
func Filter[T any](s []T, predicate func(T) bool) []T {
	var result []T
	for _, v := range s {
		if predicate(v) {
			result = append(result, v)
		}
	}
	return result
}

// Reduce reduces s to a single value using f (generics)
func Reduce[T, U any](s []T, initial U, f func(U, T) U) U {
	acc := initial
	for _, v := range s {
		acc = f(acc, v)
	}
	return acc
}

// Set is a generic set backed by a map
type Set[T comparable] struct {
	mu   sync.RWMutex
	data map[T]struct{}
}

func NewSet[T comparable]() *Set[T] {
	return &Set[T]{data: make(map[T]struct{})}
}

func (s *Set[T]) Add(v T) {
	s.mu.Lock()
	defer s.mu.Unlock()
	s.data[v] = struct{}{}
}

func (s *Set[T]) Contains(v T) bool {
	s.mu.RLock()
	defer s.mu.RUnlock()
	_, ok := s.data[v]
	return ok
}

func (s *Set[T]) Remove(v T) {
	s.mu.Lock()
	defer s.mu.Unlock()
	delete(s.data, v)
}

func (s *Set[T]) Size() int {
	s.mu.RLock()
	defer s.mu.RUnlock()
	return len(s.data)
}

// Stack is a generic LIFO stack
type Stack[T any] struct {
	items []T
	mu    sync.Mutex
}

func (s *Stack[T]) Push(v T) {
	s.mu.Lock()
	defer s.mu.Unlock()
	s.items = append(s.items, v)
}

func (s *Stack[T]) Pop() (T, bool) {
	s.mu.Lock()
	defer s.mu.Unlock()
	if len(s.items) == 0 {
		var zero T
		return zero, false
	}
	v := s.items[len(s.items)-1]
	s.items = s.items[:len(s.items)-1]
	return v, true
}

// Interface embedding
type Reader interface {
	Read(ctx context.Context, id string) ([]byte, error)
}

type Writer interface {
	Write(ctx context.Context, id string, data []byte) error
}

type Deleter interface {
	Delete(ctx context.Context, id string) error
}

// ReadWriter embeds Reader and Writer (interface embedding)
type ReadWriter interface {
	Reader
	Writer
}

// Store embeds all three operations
type Store interface {
	Reader
	Writer
	Deleter
	List(ctx context.Context) ([]string, error)
}

// SortedKeys returns map keys in sorted order (generics + Ordered constraint)
func SortedKeys[K Ordered, V any](m map[K]V) []K {
	keys := make([]K, 0, len(m))
	for k := range m {
		keys = append(keys, k)
	}
	sort.Slice(keys, func(i, j int) bool { return keys[i] < keys[j] })
	return keys
}

// Pipeline chains functions together (generics)
type Pipeline[T any] struct {
	value T
}

func NewPipeline[T any](v T) *Pipeline[T] {
	return &Pipeline[T]{value: v}
}

func (p *Pipeline[T]) Then(f func(T) T) *Pipeline[T] {
	p.value = f(p.value)
	return p
}

func (p *Pipeline[T]) Result() T {
	return p.value
}

// Example of generics with type inference
func Keys[K comparable, V any](m map[K]V) []K {
	result := make([]K, 0, len(m))
	for k := range m {
		result = append(result, k)
	}
	return result
}

func Values[K comparable, V any](m map[K]V) []V {
	result := make([]V, 0, len(m))
	for _, v := range m {
		result = append(result, v)
	}
	return result
}

func main() {
	nums := []int{5, 3, 1, 4, 2}
	doubled := Map(nums, func(n int) int { return n * 2 })
	evens := Filter(doubled, func(n int) bool { return n%2 == 0 })
	sum := Reduce(evens, 0, func(acc, n int) int { return acc + n })
	fmt.Println("sum:", sum)

	s := NewSet[string]()
	s.Add("alpha")
	s.Add("beta")
	fmt.Println("contains alpha:", s.Contains("alpha"))

	result := NewPipeline(10).
		Then(func(n int) int { return n * 2 }).
		Then(func(n int) int { return n + 5 }).
		Result()
	fmt.Println("pipeline result:", result)
}
