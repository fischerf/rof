/** @type {import('tailwindcss').Config} */
export default {
  content: ["./index.html", "./src/**/*.{js,ts,jsx,tsx}"],
  theme: {
    extend: {
      colors: {
        // ROF Bot brand palette — dark operator UI
        bg: {
          base: "#0d1117",
          surface: "#161b22",
          elevated: "#21262d",
          overlay: "#30363d",
        },
        border: {
          subtle: "#21262d",
          default: "#30363d",
          muted: "#484f58",
        },
        text: {
          primary: "#e6edf3",
          secondary: "#8b949e",
          muted: "#6e7681",
          disabled: "#484f58",
          inverse: "#0d1117",
        },
        accent: {
          blue: "#58a6ff",
          "blue-dim": "#1f6feb",
          green: "#3fb950",
          "green-dim": "#196c2e",
          yellow: "#d29922",
          "yellow-dim": "#9e6a03",
          orange: "#e3b341",
          red: "#f85149",
          "red-dim": "#8e1519",
          purple: "#bc8cff",
          pink: "#ff7b72",
          cyan: "#39d353",
        },
        // Stage status colours
        status: {
          idle: "#484f58",
          running: "#1f6feb",
          success: "#196c2e",
          failed: "#8e1519",
          paused: "#9e6a03",
          stopped: "#30363d",
          emergency: "#8e1519",
        },
        // Heatmap colours for routing view
        heatmap: {
          high: "#3fb950",    // >= 0.8 confidence
          medium: "#d29922",  // 0.5–0.8
          low: "#f85149",     // < 0.5
        },
      },
      fontFamily: {
        sans: [
          "Inter",
          "-apple-system",
          "BlinkMacSystemFont",
          "Segoe UI",
          "Helvetica",
          "Arial",
          "sans-serif",
        ],
        mono: [
          "JetBrains Mono",
          "Fira Code",
          "Cascadia Code",
          "Consolas",
          "monospace",
        ],
      },
      fontSize: {
        "2xs": ["0.625rem", { lineHeight: "0.875rem" }],
      },
      boxShadow: {
        "glow-green": "0 0 8px rgba(63, 185, 80, 0.4)",
        "glow-blue": "0 0 8px rgba(88, 166, 255, 0.4)",
        "glow-red": "0 0 8px rgba(248, 81, 73, 0.4)",
        "glow-yellow": "0 0 8px rgba(210, 153, 34, 0.4)",
        "card": "0 1px 3px rgba(0,0,0,0.4), 0 1px 2px rgba(0,0,0,0.6)",
        "card-hover": "0 4px 12px rgba(0,0,0,0.5)",
      },
      animation: {
        "pulse-slow": "pulse 3s cubic-bezier(0.4, 0, 0.6, 1) infinite",
        "spin-slow": "spin 3s linear infinite",
        "fade-in": "fadeIn 0.2s ease-in-out",
        "slide-up": "slideUp 0.2s ease-out",
        "slide-down": "slideDown 0.2s ease-out",
        "blink": "blink 1s step-end infinite",
      },
      keyframes: {
        fadeIn: {
          "0%": { opacity: "0" },
          "100%": { opacity: "1" },
        },
        slideUp: {
          "0%": { transform: "translateY(8px)", opacity: "0" },
          "100%": { transform: "translateY(0)", opacity: "1" },
        },
        slideDown: {
          "0%": { transform: "translateY(-8px)", opacity: "0" },
          "100%": { transform: "translateY(0)", opacity: "1" },
        },
        blink: {
          "0%, 100%": { opacity: "1" },
          "50%": { opacity: "0" },
        },
      },
      transitionTimingFunction: {
        "spring": "cubic-bezier(0.34, 1.56, 0.64, 1)",
      },
      borderRadius: {
        "4xl": "2rem",
      },
      spacing: {
        "18": "4.5rem",
        "88": "22rem",
        "128": "32rem",
      },
      zIndex: {
        "60": "60",
        "70": "70",
        "80": "80",
        "90": "90",
        "100": "100",
      },
      gridTemplateColumns: {
        "pipeline": "repeat(5, minmax(0, 1fr))",
        "sidebar": "1fr 320px",
        "sidebar-sm": "1fr 280px",
      },
    },
  },
  plugins: [],
};
