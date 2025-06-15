// src/components/Differentiators.tsx

import { FaUsers, FaBrain, FaLayerGroup, FaChartBar } from 'react-icons/fa';

const differentiators = [
  {
    icon: <FaUsers size={28} />,
    title: 'Multi-Agent Teamwork',
    desc: 'Assign tasks to multiple agents and observe their collaborative workflows (e.g., marketer briefs designer).',
  },
  {
    icon: <FaBrain size={28} />,
    title: 'Knowledge Hub / Memory',
    desc: 'All agents access shared company data and context—giving them persistent memory across tasks.',
  },
  {
    icon: <FaLayerGroup size={28} />,
    title: 'Workspace as UI Layer',
    desc: 'Embed the tools you already use—Slack, GDocs, Notion—into agent rooms for seamless workflow integration.',
  },
  {
    icon: <FaChartBar size={28} />,
    title: 'Agent Performance Dashboard',
    desc: 'Founders see time saved, tasks completed, and dollars saved—making the value of AI agents visible.',
  },
];

export default function Differentiators() {
  return (
    <section className="bg-purple-900 text-white py-20 px-6 text-center">
      <h2 className="text-3xl md:text-4xl font-bold mb-12">Features That Differentiate</h2>
      <div className="grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-4 gap-8 max-w-7xl mx-auto">
        {differentiators.map((item, index) => (
          <div key={index} className="bg-purple-800 p-6 rounded-xl shadow-md hover:shadow-xl transition">
            <div className="text-pink-400 mb-4 flex justify-center">{item.icon}</div>
            <h3 className="text-xl font-semibold mb-2">{item.title}</h3>
            <p className="text-sm text-purple-200">{item.desc}</p>
          </div>
        ))}
      </div>
    </section>
  );
}
