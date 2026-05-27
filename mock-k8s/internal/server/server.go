package server

import (
	"context"
	"crypto/tls"
	"errors"
	"log/slog"
	"net"
	"net/http"
	"time"
)

// New constructs an *http.Server with TLS configured from in-memory PEM.
// addr is the listen address (e.g. "0.0.0.0:6443"). handler is the mux —
// typically built by composing the apigroup + serversetscale + catch-all
// logger handlers.
func New(addr string, certPEM, keyPEM []byte, handler http.Handler) (*http.Server, error) {
	cert, err := tls.X509KeyPair(certPEM, keyPEM)
	if err != nil {
		return nil, err
	}
	return &http.Server{
		Addr:    addr,
		Handler: handler,
		TLSConfig: &tls.Config{
			Certificates: []tls.Certificate{cert},
			// K8s clients negotiate TLS 1.2+; .NET defaults to that too.
			MinVersion: tls.VersionTLS12,
		},
		ReadHeaderTimeout: 10 * time.Second,
	}, nil
}

// Run starts srv on addr with TLS, blocks until ctx is cancelled, then
// gives connections 5s to drain.
func Run(ctx context.Context, srv *http.Server) error {
	ln, err := net.Listen("tcp", srv.Addr)
	if err != nil {
		return err
	}
	slog.Info("mock-k8s API listening", "addr", srv.Addr, "tls", true)
	errCh := make(chan error, 1)
	go func() {
		errCh <- srv.ServeTLS(ln, "", "")
	}()
	select {
	case err := <-errCh:
		if errors.Is(err, http.ErrServerClosed) {
			return nil
		}
		return err
	case <-ctx.Done():
		shutdownCtx, cancel := context.WithTimeout(context.Background(), 5*time.Second)
		defer cancel()
		return srv.Shutdown(shutdownCtx)
	}
}
