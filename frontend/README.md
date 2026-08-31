# Frontend

Not scaffolded yet — deliberately.

The dashboard is Stage 4 of the plan. Stages 1–3 (real integrations, the run
loop, the exception queue) all have to land first, because a dashboard built
before there is reconciled data to put in it is a mockup with extra steps.

When it does land: Next.js app router, React, Tailwind, SSE for the timeline.
The first screen to build is *not* the one in the mockup — it is the
connect-a-source / exception-queue surface, which the mockup doesn't show and
which is the actual front door of the product.
