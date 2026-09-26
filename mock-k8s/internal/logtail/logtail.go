// Package logtail reads what a log file gained since the last read.
//
// The UE5 and Director logs mock-k8s watches are appended to for the life of
// the server and can pass 100 MB, so a reader must never re-read them: it
// seeks to the offset it stopped at and streams from there. Two properties
// every caller relies on:
//
//   - A file that SHRANK was trimmed in place by rotate-logs.sh (the writers
//     hold their logs O_APPEND, so truncation is the only rotation that
//     works). It is read again from the start; a tailer that kept its offset
//     would go deaf on exactly the busy servers whose logs get trimmed.
//   - A final line without its newline is still being written. It is left for
//     the next read, so a caller never sees half a line.
package logtail

import (
	"bufio"
	"errors"
	"io"
	"os"
)

// Tail follows one file. Not safe for concurrent use.
type Tail struct {
	path   string
	offset int64
}

func New(path string) *Tail { return &Tail{path: path} }

func (t *Tail) Path() string { return t.path }

// SkipToEnd moves past everything already in the file, for a watcher that
// only cares about what happens from now on (replaying a whole history would
// act on events long over). A missing file is not an error.
func (t *Tail) SkipToEnd() error {
	info, err := os.Stat(t.path)
	if err != nil {
		if os.IsNotExist(err) {
			return nil
		}
		return err
	}
	t.offset = info.Size()
	return nil
}

// Scan calls fn with each complete line appended since the previous Scan,
// newline included. fn must not keep the slice. A missing file is not an
// error: the writer may not have started yet.
func (t *Tail) Scan(fn func(line []byte)) error {
	f, err := os.Open(t.path)
	if err != nil {
		if os.IsNotExist(err) {
			return nil
		}
		return err
	}
	defer f.Close()
	info, err := f.Stat()
	if err != nil {
		return err
	}
	if info.Size() < t.offset {
		t.offset = 0
	}
	if info.Size() == t.offset {
		return nil
	}
	if _, err := f.Seek(t.offset, io.SeekStart); err != nil {
		return err
	}
	r := bufio.NewReaderSize(f, 64*1024)
	for {
		line, err := r.ReadBytes('\n')
		if errors.Is(err, io.EOF) {
			return nil // any partial line stays unread
		}
		if err != nil {
			return err
		}
		t.offset += int64(len(line))
		fn(line)
	}
}
