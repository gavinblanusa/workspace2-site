# Workspace2.0

This project uses React and Vite.

## Development

Install dependencies and start the dev server:

```bash
npm install
npm run dev
```

## Environment variables

To enable waitlist submissions through Supabase, set the following variables in a `.env` file or through your hosting platform:

```
VITE_SUPABASE_URL=<your-supabase-url>
VITE_SUPABASE_ANON_KEY=<your-supabase-anon-key>
```

If these are not provided, the site will still render but submissions will be skipped.

## Production build

To create an optimized build and preview it locally:

```bash
npm run build
npm run preview
```
