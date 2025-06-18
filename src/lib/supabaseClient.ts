import { createClient, type SupabaseClient } from '@supabase/supabase-js';

// Vite injects env vars on build via `import.meta.env`.
// We support both `VITE_` and `NEXT_PUBLIC_` prefixes so the code
// works locally (Vite) and on Vercel / Next.js.
const {
  VITE_SUPABASE_URL,
  NEXT_PUBLIC_SUPABASE_URL,
  VITE_SUPABASE_ANON_KEY,
  NEXT_PUBLIC_SUPABASE_ANON_KEY,
} = import.meta.env as Record<string, string | undefined>;

const supabaseUrl = VITE_SUPABASE_URL ?? NEXT_PUBLIC_SUPABASE_URL ?? '';
const supabaseAnonKey =
  VITE_SUPABASE_ANON_KEY ?? NEXT_PUBLIC_SUPABASE_ANON_KEY ?? '';

let supabase: SupabaseClient | null = null;

if (supabaseUrl && supabaseAnonKey) {
  supabase = createClient(supabaseUrl, supabaseAnonKey);
} else {
  console.warn('Supabase environment variables missing; client not initialized.');
}

export { supabase };
