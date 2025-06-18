import { createClient } from '@supabase/supabase-js';

// Vite exposes environment variables through `import.meta.env` in the browser
// context. Using `process.env` causes a runtime error because `process` is not
// defined when the code runs in the browser. The original code attempted to
// read the Supabase credentials via `process.env`, which resulted in an
// exception and caused the site to render a blank page. By switching to
// `import.meta.env` we ensure the values are replaced at build time.

// Support both the `VITE_` and `NEXT_PUBLIC_` prefixes so the project works
// whether variables are defined in a Vite-style `.env` file or in a Vercel
// environment configured for Next.js. Vite only exposes variables with
// prefixes listed in `envPrefix` (see `vite.config.ts`).
const {
  VITE_SUPABASE_URL,
  NEXT_PUBLIC_SUPABASE_URL,
  VITE_SUPABASE_ANON_KEY,
  NEXT_PUBLIC_SUPABASE_ANON_KEY
} = import.meta.env;

const supabaseUrl = VITE_SUPABASE_URL || NEXT_PUBLIC_SUPABASE_URL || '';
const supabaseAnonKey =
  VITE_SUPABASE_ANON_KEY || NEXT_PUBLIC_SUPABASE_ANON_KEY || '';

export const supabase = createClient(supabaseUrl, supabaseAnonKey);
