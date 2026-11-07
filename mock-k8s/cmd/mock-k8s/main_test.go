package main

import (
	"testing"
	"time"
)

func TestParseReconcileInterval(t *testing.T) {
	cases := []struct {
		in   string
		want time.Duration
	}{
		{"", 30 * time.Second},        // default
		{"45s", 45 * time.Second},     // explicit
		{"1m", time.Minute},           // explicit
		{"0", 0},                      // disabled
		{"off", 0},                    // disabled
		{"garbage", 30 * time.Second}, // unparseable -> default
		{"-5s", 0},                    // non-positive -> disabled
		{"OFF", 0},                    // case-insensitive disable
		{"False", 0},                  // mixed-case disable
		{" 45s ", 45 * time.Second},   // whitespace-padded (regression guard)
		{"  off  ", 0},                // whitespace + disable
	}
	for _, c := range cases {
		if got := parseReconcileInterval(c.in); got != c.want {
			t.Errorf("parseReconcileInterval(%q) = %v, want %v", c.in, got, c.want)
		}
	}
}

func TestParseDurationEnv(t *testing.T) {
	const def = 2 * time.Minute
	for _, tc := range []struct {
		raw  string
		want time.Duration
	}{
		{"", def},
		{"  ", def},
		{"off", 0},
		{"OFF", 0},
		{"disabled", 0},
		{"0", 0},
		{"-5s", 0},
		{"90s", 90 * time.Second},
		{" 5m ", 5 * time.Minute},
		{"soon", def},
	} {
		if got := parseDurationEnv("X", tc.raw, def); got != tc.want {
			t.Errorf("parseDurationEnv(%q) = %v, want %v", tc.raw, got, tc.want)
		}
	}
}

func TestParseCoriolisDelay_NeverZero(t *testing.T) {
	for _, raw := range []string{"", "off", "0", "-5s", "soon"} {
		if got := parseCoriolisDelay(raw); got != 2*time.Minute {
			t.Errorf("parseCoriolisDelay(%q) = %v, want the 2m default", raw, got)
		}
	}
	if got := parseCoriolisDelay("5m"); got != 5*time.Minute {
		t.Errorf("parseCoriolisDelay(5m) = %v", got)
	}
}

func TestParseCapBroadcast(t *testing.T) {
	for raw, want := range map[string]bool{"": true, "on": true, "1": true, "off": false, "OFF": false, "0": false, "false": false, "disabled": false} {
		if got := parseCapBroadcast(raw); got != want {
			t.Errorf("parseCapBroadcast(%q) = %v, want %v", raw, got, want)
		}
	}
}

// The mock API has no auth and can start or stop game servers, so a missing
// or malformed bind must never widen to all interfaces.
func TestListenAddrDefaultsToLoopback(t *testing.T) {
	cases := []struct {
		bind string
		want string
	}{
		{"", "127.0.0.1:6443"},
		{"   ", "127.0.0.1:6443"},
		{"not-an-ip", "127.0.0.1:6443"},
		{"0.0.0.0:80", "127.0.0.1:6443"},
		{"127.0.0.1", "127.0.0.1:6443"},
		{"0.0.0.0", "0.0.0.0:6443"},
		{" 10.0.0.5 ", "10.0.0.5:6443"},
		{"::1", "[::1]:6443"},
	}
	for _, c := range cases {
		if got := listenAddr(c.bind, 6443); got != c.want {
			t.Errorf("listenAddr(%q) = %q, want %q", c.bind, got, c.want)
		}
	}
}
