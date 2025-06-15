// src/components/Pricing.tsx

//import React from 'react';

const plans = [
  {
    title: 'Freemium',
    price: '$0/mo',
    features: [
      '1 Virtual Room',
      '1 Base-Tier AI Agent',
      'Limited Simulations',
      'Community Support',
    ],
    button: 'Get Started',
    highlighted: false,
  },
  {
    title: 'Pro',
    price: '$50/mo',
    features: [
      'Multiple Rooms',
      '3 AI Agents (Any Tier)',
      'Access to Agent Marketplace',
      'Workspace Customization',
      'Standard Simulations',
    ],
    button: 'Get Started',
    highlighted: true,
  },
  {
    title: 'Founder+',
    price: '$150/mo',
    features: [
      'Unlimited Rooms',
      '5+ AI Agents',
      'Full Simulation Tools',
      'Custom Integrations (Zapier, etc.)',
      'Priority Support',
    ],
    button: 'Get Started',
    highlighted: false,
  },
  {
    title: 'Teams',
    price: 'Custom',
    features: [
      '2+ Human Users',
      'Agent Coordination Layer',
      'Advanced Reporting',
      'Shared Knowledge Hub',
      'Dedicated Success Manager',
    ],
    button: 'Contact Sales',
    highlighted: false,
  },
];

export default function Pricing() {
  return (
    <section className="bg-purple-950 text-white py-20 px-6 text-center">
      <h2 className="text-3xl md:text-4xl font-bold mb-12">Pricing: Scale As You Grow</h2>
      <div className="grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-4 gap-8 max-w-7xl mx-auto">
        {plans.map((plan, index) => (
          <div
            key={index}
            className={`rounded-xl p-6 shadow-md ${
              plan.highlighted ? 'bg-pink-600 text-white scale-[1.03]' : 'bg-purple-800 text-purple-100'
            } transition`}
          >
            {plan.highlighted && (
              <div className="bg-pink-400 text-pink-900 text-xs font-semibold px-3 py-1 rounded-full inline-block mb-3">
                Most Popular
              </div>
            )}
            <h3 className="text-xl font-bold mb-1">{plan.title}</h3>
            <p className="text-lg font-semibold mb-4">{plan.price}</p>
            <ul className="text-sm text-left space-y-2 mb-6">
              {plan.features.map((f, i) => (
                <li key={i}>⚡ {f}</li>
              ))}
            </ul>
            <button className="bg-gradient-to-r from-pink-500 to-purple-500 text-white px-5 py-2 rounded-xl hover:scale-105 transition">
              {plan.button}
            </button>
          </div>
        ))}
      </div>
    </section>
  );
}
