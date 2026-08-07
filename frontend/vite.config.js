import { defineConfig } from 'vite';
import react from '@vitejs/plugin-react';
export default defineConfig({
    plugins: [react()],
    server: {
        port: 5173,
        host: '127.0.0.1',
        proxy: {
            '/api': { target: 'http://127.0.0.1:8602', changeOrigin: true },
            '/ws': { target: 'ws://127.0.0.1:8602', ws: true, changeOrigin: true },
        },
    },
    build: {
        outDir: 'dist',
        chunkSizeWarningLimit: 1500,
        rollupOptions: {
            output: {
                manualChunks: {
                    echarts: ['echarts', 'echarts-for-react'],
                    'react-vendor': ['react', 'react-dom', 'react-router-dom', '@tanstack/react-query', '@tanstack/react-table'],
                },
            },
        },
    },
});
