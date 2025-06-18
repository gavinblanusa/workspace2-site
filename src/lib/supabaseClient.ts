import { createClient } from '@supabase/supabase-js';

// Vite exposes environment variables through `import.meta.env` in the browser
// context. Using `process.env` causes a runtime error because `process` is not
// defined when the code runs in the browser. The original code attempted to
// read the Supabase credentials via `process.env`, which resulted in an
// exception and caused the site to render a blank page. By switching to
// `import.meta.env` we ensure the values are replaced at build time.

const supabaseUrl = import.meta.env.VITE_SUPABASE_URL || '';
const supabaseAnonKey = import.meta.env.VITE_SUPABASE_ANON_KEY || '';

export const supabase = createClient(supabaseUrl, supabaseAnonKey);
