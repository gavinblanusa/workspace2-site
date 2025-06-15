import Header from '../components/Header';
import Hero from '../components/Hero';
import Features from '../components/Features';
import Differentiators from '../components/Differentiators';
import Pricing from '../components/Pricing';
import CTA from '../components/CTA';
import Footer from '../components/Footer';

export default function IndexPage() {
  return (
    <>
      <Header />
      <main>
        <Hero />
        <section id="features">
          <Features />
          <Differentiators />
        </section>
        <section id="pricing">
          <Pricing />
        </section>
        <section id="cta">
          <CTA />
        </section>
      </main>
      <Footer />
    </>
  );
}
