# Workspace2.0

This project uses React and Vite.

## Development

Install dependencies and start the dev server:

```bash
npm install
npm run dev
```

When the Vite dev server starts, it prints the local URL and port, such as
`http://localhost:5173`. If you are running the project inside a container or VM,
make sure this port is forwarded or opened so the server is reachable from your
host machine. For remote access you can run:

```bash
npm run dev -- --host
```

## Environment variables

To enable waitlist submissions through Supabase, set the following variables in a `.env` file or through your hosting platform:

```
VITE_SUPABASE_URL=<your-supabase-url>
VITE_SUPABASE_ANON_KEY=<your-supabase-anon-key>
```

