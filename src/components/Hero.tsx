//import React from 'react';

// src/components/Hero.tsx

export default function Hero() {
    return (
      <section className="bg-gradient-to-b from-purple-900 to-purple-800 text-white py-24 text-center px-6">
        <h1 className="text-4xl md:text-6xl font-bold tracking-tight mb-6">
          Your AI <span className="bg-gradient-to-r from-pink-500 to-purple-400 text-transparent bg-clip-text">C‑Suite Awaits</span>
        </h1>
        <p className="text-lg md:text-xl max-w-2xl mx-auto mb-8">
          Design your virtual HQ, hire expert AI agents, and orchestrate your business. Workspace2.0 is the interaction layer for tomorrow's hyper-efficient solo founders & lean teams.
        </p>
        <button className="bg-gradient-to-r from-pink-500 to-purple-500 px-6 py-3 rounded-xl text-white text-lg font-semibold hover:scale-105 transition">
          Launch Your AI-Powered Office
        </button>
      </section>
    );
  }
  
