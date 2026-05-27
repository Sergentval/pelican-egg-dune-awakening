// Package sa writes the in-cluster ServiceAccount mount that the Funcom
// Battlegroup Director's K8s client reads at startup.
//
// The Director uses the standard Kubernetes in-cluster client convention:
//
//	/var/run/secrets/kubernetes.io/serviceaccount/token       — bearer token
//	/var/run/secrets/kubernetes.io/serviceaccount/ca.crt      — trusted CA for KUBERNETES_SERVICE_HOST
//	/var/run/secrets/kubernetes.io/serviceaccount/namespace   — pod's namespace
//
// We control both ends (mock-k8s issues the TLS cert AND signs the token),
// so the contents don't have to be cryptographically meaningful — just
// well-formed. The Director's client validates the bearer token against
// our server's response and the CA cert against the server's TLS cert.
package sa

import (
	"fmt"
	"os"
	"path/filepath"
)

// MountPath is the canonical in-cluster ServiceAccount directory.
const MountPath = "/var/run/secrets/kubernetes.io/serviceaccount"

// Files defines the contents written to MountPath.
type Files struct {
	// Token is the bearer credential the Director sends as `Authorization: Bearer <Token>`.
	Token string
	// CACert is the PEM-encoded CA certificate that the Director uses to verify our TLS server.
	CACert []byte
	// Namespace is what the Director treats as its default namespace.
	Namespace string
}

// Write atomically populates MountPath with token/ca.crt/namespace.
// Caller is responsible for ensuring MountPath exists and is writable
// (handled by the runtime Dockerfile's VOLUME directive).
func Write(f Files) error {
	if err := os.MkdirAll(MountPath, 0o755); err != nil {
		return fmt.Errorf("mkdir %s: %w", MountPath, err)
	}
	writes := []struct {
		name string
		data []byte
		mode os.FileMode
	}{
		{"token", []byte(f.Token), 0o600},
		{"ca.crt", f.CACert, 0o644},
		{"namespace", []byte(f.Namespace), 0o644},
	}
	for _, w := range writes {
		dst := filepath.Join(MountPath, w.name)
		// Write to a temp file then rename so readers never see a partial file.
		tmp := dst + ".tmp"
		if err := os.WriteFile(tmp, w.data, w.mode); err != nil {
			return fmt.Errorf("write %s: %w", tmp, err)
		}
		if err := os.Rename(tmp, dst); err != nil {
			return fmt.Errorf("rename %s -> %s: %w", tmp, dst, err)
		}
	}
	return nil
}
