/**
 * Passenger entrypoint for MochaHost's cPanel "Setup Node.js App".
 *
 * cPanel's Node Application Manager (Phusion Passenger) runs this file
 * directly and expects the app to listen on the port Passenger assigns
 * via process.env.PORT — `next start` on its own does not read that
 * convention, so this wraps the production Next.js server the way
 * cPanel's own Node.js docs describe.
 *
 * Requires a production build (`npm run build`) to already exist on
 * disk — see docs/technical/mochahost-deployment.md.
 */
const { createServer } = require("http");
const next = require("next");

const port = process.env.PORT || 3000;
const app = next({ dev: false });
const handle = app.getRequestHandler();

app
  .prepare()
  .then(() => {
    createServer((req, res) => handle(req, res)).listen(port, () => {
      console.log(`web server listening on ${port}`);
    });
  })
  .catch((err) => {
    console.error("failed to start web server", err);
    process.exit(1);
  });
