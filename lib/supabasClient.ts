// src/lib/supabaseClient.ts
import { createClient } from '@supabase/supabase-js';

const supabaseUrl = 'https://aotdddgoudzcykkgeojf.supabase.co'; // 
const supabaseAnonKey = 'eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJpc3MiOiJzdXBhYmFzZSIsInJlZiI6ImFvdGRkZGdvdWR6Y3lra2dlb2pmIiwicm9sZSI6ImFub24iLCJpYXQiOjE3NTAwMDY2MzksImV4cCI6MjA2NTU4MjYzOX0.v6uiDStU2DRGJLXYwmIMe7mKDQO60pinF-15oBlobfc'; // 

export const supabase = createClient(supabaseUrl, supabaseAnonKey);
