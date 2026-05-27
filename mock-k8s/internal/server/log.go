package server

import (
	"bytes"
	"io"
	"log/slog"
	"net/http"
)

// LogMiddleware wraps next with structured request/response logging.
// This is the instrument-first observation surface: every method+path the
// Director hits gets logged so we know what endpoints to specialize next.
// Bodies up to bodyLogLimit bytes are captured for unhandled routes.
func LogMiddleware(next http.Handler, bodyLogLimit int64) http.Handler {
	return http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		// Capture body cheaply — many K8s requests are POST/PATCH with JSON.
		// We only need it for unhandled routes (404), so peek without
		// consuming.
		var bodyBytes []byte
		if r.Body != nil && r.ContentLength > 0 && r.ContentLength <= bodyLogLimit {
			bodyBytes, _ = io.ReadAll(io.LimitReader(r.Body, bodyLogLimit))
			r.Body = io.NopCloser(bytes.NewReader(bodyBytes))
		}

		// Snoop status code by wrapping ResponseWriter.
		rec := &statusRecorder{ResponseWriter: w, status: 200}
		next.ServeHTTP(rec, r)

		attrs := []any{
			"method", r.Method,
			"path", r.URL.Path,
			"query", r.URL.RawQuery,
			"status", rec.status,
			"remote", r.RemoteAddr,
			"ua", r.Header.Get("User-Agent"),
		}
		if rec.status == http.StatusNotFound && len(bodyBytes) > 0 {
			attrs = append(attrs, "body", string(bodyBytes))
		}
		level := slog.LevelInfo
		if rec.status >= 500 {
			level = slog.LevelError
		} else if rec.status == http.StatusNotFound {
			level = slog.LevelWarn
		}
		slog.LogAttrs(r.Context(), level, "http", attrsToSlog(attrs)...)
	})
}

type statusRecorder struct {
	http.ResponseWriter
	status int
}

func (s *statusRecorder) WriteHeader(code int) {
	s.status = code
	s.ResponseWriter.WriteHeader(code)
}

// attrsToSlog converts an alternating []any to []slog.Attr.
func attrsToSlog(kv []any) []slog.Attr {
	out := make([]slog.Attr, 0, len(kv)/2)
	for i := 0; i+1 < len(kv); i += 2 {
		k, _ := kv[i].(string)
		out = append(out, slog.Any(k, kv[i+1]))
	}
	return out
}
