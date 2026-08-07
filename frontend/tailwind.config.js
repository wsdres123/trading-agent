/** @type {import('tailwindcss').Config} */
export default {
  content: ['./index.html', './src/**/*.{js,ts,jsx,tsx}'],
  theme: {
    extend: {
      colors: {
        bg: '#0e0e0e',
        panel: '#1a1a1a',
        text: '#ffffff',
        stock: '#ffd500',
        up: '#ff3b3b',
        down: '#22c55e',
        muted: '#9ca3af',
        accent: '#1a56db',
        border: '#333333',
      },
      fontFamily: {
        sans: "'Microsoft YaHei','PingFang SC','Noto Sans CJK SC',sans-serif",
      },
      fontSize: {
        base: ['18px', '1.6'],
      },
    },
  },
  plugins: [],
}
