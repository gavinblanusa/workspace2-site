// src/components/Features.tsx

//import React from 'react';
import { FaNetworkWired, FaRobot, FaTools, FaPlayCircle } from 'react-icons/fa';

const features = [
  {
    icon: <FaNetworkWired size={28} />,
    title: 'Web-Based Virtual Office',
    desc: 'Design a 2D interactive floor plan—each room is a dedicated AI agent or workspace (e.g., Sales, Design, Ops).',
  },
  {
    icon: <FaRobot size={28} />,
    title: 'Agent Hiring Interface',
    desc: "Hire an 'AI CFO' or 'AI Marketer' and choose from partner providers. Browse tiers like Intern, Mid, and Expert.",
  },
  {
    icon: <FaTools size={28} />,
    title: 'Workspace Interaction',
    desc: 'Embed tools like Google Docs & Figma. Click an agent’s desk to see what they’re doing or give instructions.',
  },
  {
    icon: <FaPlayCircle size={28} />,
    title: 'Run Simulations',
    desc: 'Test agents: simulate sales calls, shadow workflows, or preview how they handle real business tasks.',
  },
];

export default function Features() {
  return (
    <section className="bg-purple-950 text-white py-20 px-6 text-center">
      <h2 className="text-3xl md:text-4xl font-bold mb-12">Core MVP: Build Your Dream Team</h2>
      <div className="grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-4 gap-8 max-w-7xl mx-auto">
        {features.map((feat, index) => (
          <div key={index} className="bg-purple-800 p-6 rounded-xl shadow-md hover:shadow-xl transition">
            <div className="text-pink-400 mb-4 flex justify-center">{feat.icon}</div>
            <h3 className="text-xl font-semibold mb-2">{feat.title}</h3>
            <p className="text-sm text-purple-200">{feat.desc}</p>
          </div>
        ))}
      </div>
    </section>
  );
}
