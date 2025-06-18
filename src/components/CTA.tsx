// src/components/CTA.tsx

import React, { useState } from 'react';
import { supabase } from '../lib/supabaseClient';

export default function CTA() {
  const [email, setEmail] = useState('');
  const [submitted, setSubmitted] = useState(false);

  const handleSubmit = async (e: React.FormEvent<HTMLFormElement>) => {
    e.preventDefault();

    const { error } = await supabase.from('waitlist').insert([{ email }]);

    if (error) {
      console.error('Submission error:', error);
      return;
    }

    setSubmitted(true);
  };

  return (
    <section className="bg-gradient-to-r from-purple-800 to-purple-700 text-white py-20 px-6 text-center">
      <h2 className="text-3xl md:text-4xl font-bold mb-6">Be the First to Hire Your AI C‑Suite</h2>
      <p className="mb-8 max-w-xl mx-auto text-purple-200">
        Join the waitlist to be notified when Workspace2.0 launches and get early access perks.
      </p>
      {!submitted ? (
        <form onSubmit={handleSubmit} className="flex flex-col sm:flex-row justify-center gap-4 max-w-md mx-auto">
          <input
            type="email"
            placeholder="your@email.com"
            value={email}
            onChange={(e) => setEmail(e.target.value)}
            className="w-full px-4 py-3 rounded-lg text-black text-sm"
            required
          />
          <button
            type="submit"
            className="bg-pink-500 hover:bg-pink-600 px-6 py-3 rounded-lg text-white text-sm font-semibold transition"
          >
            Join Waitlist
          </button>
        </form>
      ) : (
        <p className="text-green-300 font-semibold">You're on the list! We'll be in touch. 🚀</p>
      )}
    </section>
  );
}

