// src/components/Header.tsx

//import React from 'react';

export default function Header() {
  return (
    <header className="sticky top-0 bg-purple-950 z-50 shadow-sm">
      <div className="max-w-7xl mx-auto px-6 py-4 flex justify-between items-center text-white">
        <h1 className="text-xl font-bold tracking-tight">Workspace2.0</h1>
        <nav className="space-x-6 text-sm font-medium">
          <a href="#features" className="hover:text-pink-400">Features</a>
          <a href="#pricing" className="hover:text-pink-400">Pricing</a>
          <a href="#cta" className="bg-pink-500 px-4 py-2 rounded-md hover:bg-pink-600 transition">Join Waitlist</a>
        </nav>
      </div>
    </header>
  );
}
