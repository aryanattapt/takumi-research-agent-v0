export default {
  content: ["./index.html", "./src/**/*.{ts,tsx}"],
  theme: {
    extend: {
      colors: {
        ink: "#172033",
        mist: "#f3f6fb",
        teal: { 50: "#eafaf8", 500: "#19a896", 600: "#128676" },
      },
      boxShadow: { panel: "0 12px 36px rgba(25, 45, 78, 0.08)" },
    },
  },
  plugins: [],
};
