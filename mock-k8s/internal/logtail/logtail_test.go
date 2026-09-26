package logtail

import (
	"os"
	"path/filepath"
	"strings"
	"testing"
)

func appendTo(t *testing.T, path, s string) {
	t.Helper()
	f, err := os.OpenFile(path, os.O_CREATE|os.O_APPEND|os.O_WRONLY, 0o644)
	if err != nil {
		t.Fatal(err)
	}
	defer f.Close()
	if _, err := f.WriteString(s); err != nil {
		t.Fatal(err)
	}
}

func collect(t *testing.T, tl *Tail) []string {
	t.Helper()
	var got []string
	if err := tl.Scan(func(line []byte) { got = append(got, strings.TrimRight(string(line), "\n")) }); err != nil {
		t.Fatal(err)
	}
	return got
}

func TestScan_OnlyNewCompleteLines(t *testing.T) {
	p := filepath.Join(t.TempDir(), "x.log")
	appendTo(t, p, "a\nb\npart")
	tl := New(p)
	if got := collect(t, tl); strings.Join(got, ",") != "a,b" {
		t.Fatalf("first scan = %v, want [a b] (the unterminated line waits)", got)
	}
	appendTo(t, p, "ial\nc\n")
	if got := collect(t, tl); strings.Join(got, ",") != "partial,c" {
		t.Fatalf("second scan = %v, want [partial c]", got)
	}
	if got := collect(t, tl); len(got) != 0 {
		t.Fatalf("third scan = %v, want nothing new", got)
	}
}

// rotate-logs.sh trims logs in place: a shrunk file is read again from the
// start rather than never again.
func TestScan_ShrunkFileIsReadFromStart(t *testing.T) {
	p := filepath.Join(t.TempDir(), "x.log")
	appendTo(t, p, "one\ntwo\nthree\n")
	tl := New(p)
	collect(t, tl)
	if err := os.WriteFile(p, []byte("fresh\n"), 0o644); err != nil {
		t.Fatal(err)
	}
	if got := collect(t, tl); strings.Join(got, ",") != "fresh" {
		t.Fatalf("after trim = %v, want [fresh]", got)
	}
}

func TestScan_MissingFileIsNotAnError(t *testing.T) {
	tl := New(filepath.Join(t.TempDir(), "nope.log"))
	if got := collect(t, tl); len(got) != 0 {
		t.Fatalf("missing file yielded %v", got)
	}
}

func TestSkipToEnd_IgnoresExistingContent(t *testing.T) {
	p := filepath.Join(t.TempDir(), "x.log")
	appendTo(t, p, "old\n")
	tl := New(p)
	if err := tl.SkipToEnd(); err != nil {
		t.Fatal(err)
	}
	appendTo(t, p, "new\n")
	if got := collect(t, tl); strings.Join(got, ",") != "new" {
		t.Fatalf("after SkipToEnd = %v, want [new]", got)
	}
}
