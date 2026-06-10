/** Tailwind build config — compiled to static/app.css at image build time so the
 *  dashboard needs no CDN and works fully offline. The safelist covers colour
 *  classes that the dashboard JS builds dynamically (status + feature palettes). */
module.exports = {
  content: ["./static/**/*.html"],
  safelist: [
    { pattern: /(bg|text|border|decoration)-(slate|rose|indigo|purple|amber|red|emerald|blue|cyan|green|orange|yellow|teal)-(50|100|200|300|400|500|600|700|800|900)/ },
    { pattern: /(grid-cols)-(1|2|3|4|6|8|10)/, variants: ["sm", "md", "lg", "xl"] },
  ],
  theme: { extend: {} },
  plugins: [],
};
