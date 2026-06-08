/** @type {import('tailwindcss').Config} */
// Colors remapped to the "Deep Desert" OKLCH palette (Claude Design handoff).
// The slate ramp = warm desert surfaces/text; spice = the orange accent. Each
// value carries <alpha-value> so Tailwind's /opacity modifiers still work
// (e.g. bg-slate-900/80). The richer, theme-swappable surfaces live in the
// CSS-variable component classes in index.css; these utilities are the
// desert-default so the thousands of existing slate-*/spice-* utilities across
// the app read warm without a per-component rewrite.
export default {
  content: ["./index.html", "./src/**/*.{ts,tsx}"],
  theme: {
    extend: {
      colors: {
        // warm desert neutral ramp (replaces Tailwind's cool slate)
        slate: {
          50: "oklch(0.970 0.010 84 / <alpha-value>)",
          100: "oklch(0.945 0.018 82 / <alpha-value>)",
          200: "oklch(0.880 0.018 80 / <alpha-value>)",
          300: "oklch(0.805 0.020 78 / <alpha-value>)",
          400: "oklch(0.730 0.022 74 / <alpha-value>)",
          500: "oklch(0.585 0.022 68 / <alpha-value>)",
          600: "oklch(0.430 0.026 62 / <alpha-value>)",
          700: "oklch(0.330 0.028 60 / <alpha-value>)",
          800: "oklch(0.262 0.028 58 / <alpha-value>)",
          900: "oklch(0.214 0.024 60 / <alpha-value>)",
          950: "oklch(0.165 0.018 64 / <alpha-value>)",
        },
        // spice = the desert accent (warm orange → ember)
        spice: {
          50: "oklch(0.965 0.030 70 / <alpha-value>)",
          100: "oklch(0.930 0.055 68 / <alpha-value>)",
          200: "oklch(0.880 0.090 65 / <alpha-value>)",
          300: "oklch(0.815 0.130 62 / <alpha-value>)",
          400: "oklch(0.780 0.155 60 / <alpha-value>)",
          500: "oklch(0.745 0.165 58 / <alpha-value>)",
          600: "oklch(0.700 0.180 52 / <alpha-value>)",
          700: "oklch(0.660 0.190 42 / <alpha-value>)",
          800: "oklch(0.560 0.165 40 / <alpha-value>)",
          900: "oklch(0.470 0.130 42 / <alpha-value>)",
        },
      },
      fontFamily: {
        sans: ["Hanken Grotesk", "ui-sans-serif", "system-ui", "sans-serif"],
        display: ["Saira Condensed", "Oswald", "system-ui", "sans-serif"],
        mono: ["JetBrains Mono", "ui-monospace", "SFMono-Regular", "Menlo", "monospace"],
      },
      animation: {
        "fade-in": "fadeIn 0.15s ease-out",
        "slide-in-right": "slideInRight 0.2s ease-out",
      },
      keyframes: {
        fadeIn: {
          "0%": { opacity: "0" },
          "100%": { opacity: "1" },
        },
        slideInRight: {
          "0%": { transform: "translateX(100%)", opacity: "0" },
          "100%": { transform: "translateX(0)", opacity: "1" },
        },
      },
    },
  },
  plugins: [],
};
