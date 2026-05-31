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
	}
	for _, c := range cases {
		if got := parseReconcileInterval(c.in); got != c.want {
			t.Errorf("parseReconcileInterval(%q) = %v, want %v", c.in, got, c.want)
		}
	}
}
