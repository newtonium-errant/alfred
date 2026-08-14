/** @type {import('next').NextConfig} */
const nextConfig = {
  reactStrictMode: true,
  experimental: {
    // Loads `instrumentation.ts` at server boot, which is where the push poller
    // is armed. In Next 14.2 this hook is still experimental and defaults to
    // FALSE — verified in the installed 14.2.35 (`next/dist/server/
    // config-shared.js` sets `instrumentationHook: false` inside the
    // experimental block; the zod schema accepts it only there).
    //
    // So this line is not optional decoration: without it Next never loads
    // instrumentation.ts, `register()` is never called, and the boot arming is
    // dead code that every unit test still passes. `pushBootArming.test.ts`
    // asserts this flag against the config OBJECT for that reason — the pin is
    // on the wiring, not on the function.
    //
    // The flag became stable in Next 15; this comment can go with that upgrade.
    instrumentationHook: true,
  },
}
module.exports = nextConfig
