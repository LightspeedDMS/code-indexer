package pathological

import (
	"fmt"
	"strings"
)

// Very long single line with deeply chained calls
func deepChain(s string) string { return strings.TrimSpace(strings.ToLower(strings.ReplaceAll(strings.ReplaceAll(strings.TrimRight(strings.TrimLeft(s, " \t"), " \t"), "  ", " "), "\n", " "))) }

// Deeply nested if-else tree
func classify(x, y, z int) string {
	if x > 0 {
		if y > 0 {
			if z > 0 {
				return "all positive"
			} else if z == 0 {
				return "x,y positive z zero"
			} else {
				return "x,y positive z negative"
			}
		} else if y == 0 {
			if z > 0 {
				return "x positive y zero z positive"
			} else {
				return "x positive y zero z non-positive"
			}
		} else {
			if z > 0 {
				return "x positive y negative z positive"
			} else if z == 0 {
				return "x positive y negative z zero"
			} else {
				return "all negative except x"
			}
		}
	} else if x == 0 {
		if y > 0 {
			return "x zero y positive"
		} else if y == 0 {
			if z == 0 {
				return "all zero"
			}
			return "x,y zero z nonzero"
		}
		return "x zero y negative"
	}
	return "x negative"
}

// Many parameters
func buildQuery(table, alias string, cols []string, conditions []string, orderBy string, asc bool, limit, offset int) string {
	colStr := "*"
	if len(cols) > 0 {
		colStr = strings.Join(cols, ", ")
	}
	q := fmt.Sprintf("SELECT %s FROM %s AS %s", colStr, table, alias)
	if len(conditions) > 0 {
		q += " WHERE " + strings.Join(conditions, " AND ")
	}
	if orderBy != "" {
		dir := "DESC"
		if asc {
			dir = "ASC"
		}
		q += fmt.Sprintf(" ORDER BY %s %s", orderBy, dir)
	}
	if limit > 0 {
		q += fmt.Sprintf(" LIMIT %d OFFSET %d", limit, offset)
	}
	return q
}

// Nested closures
func makeMultiplier(factor int) func(int) func(int) int {
	return func(base int) func(int) int {
		return func(x int) int {
			return x * base * factor
		}
	}
}

// Select statement with many cases
func handleEvent(event string, ch1, ch2, ch3 chan int, done chan struct{}) string {
	select {
	case v := <-ch1:
		return fmt.Sprintf("ch1: %d event: %s", v, event)
	case v := <-ch2:
		return fmt.Sprintf("ch2: %d event: %s", v, event)
	case v := <-ch3:
		return fmt.Sprintf("ch3: %d event: %s", v, event)
	case <-done:
		return "done"
	default:
		return "no data"
	}
}

// Switch with many cases and fallthrough
func dayName(n int) string {
	switch n {
	case 1:
		return "Monday"
	case 2:
		return "Tuesday"
	case 3:
		return "Wednesday"
	case 4:
		return "Thursday"
	case 5:
		return "Friday"
	case 6:
		return "Saturday"
	case 7:
		return "Sunday"
	default:
		return "Unknown"
	}
}

// Struct with many fields
type BigStruct struct {
	F1, F2, F3, F4, F5, F6, F7, F8, F9, F10 int
	S1, S2, S3, S4, S5                        string
	B1, B2, B3                                bool
	Nested                                    *BigStruct
}

// Recursive type definition
type Tree struct {
	Value    interface{}
	Children []*Tree
}

func (t *Tree) Depth() int {
	if len(t.Children) == 0 {
		return 0
	}
	maxChild := 0
	for _, child := range t.Children {
		if d := child.Depth(); d > maxChild {
			maxChild = d
		}
	}
	return maxChild + 1
}
