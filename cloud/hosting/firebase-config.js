// Firebase web config for the license portal.
//
// Fill these in from: Firebase console -> Project settings -> "Your apps" ->
// Web app -> SDK setup and configuration -> "Config".
//
// These values are NOT secrets. Web API keys are meant to be public; the
// portal is kept safe by Firebase Auth + the Firestore security rules
// (cloud/firestore.rules), not by hiding this config.
export const firebaseConfig = {
  apiKey: "REPLACE_ME",
  authDomain: "REPLACE_ME.firebaseapp.com",
  projectId: "REPLACE_ME",
  storageBucket: "REPLACE_ME.appspot.com",
  messagingSenderId: "REPLACE_ME",
  appId: "REPLACE_ME",
};

// Region the Cloud Functions are deployed to (see cloud/functions/main.py).
export const functionsRegion = "us-central1";

// Base URL for the HTTP functions (create_checkout_session lives here). After
// `firebase deploy`, the CLI prints each HTTP function's URL; use the common
// prefix, e.g. "https://us-central1-<project>.cloudfunctions.net".
export const functionsBaseUrl = "https://us-central1-REPLACE_ME.cloudfunctions.net";

// Products shown in the storefront. Add an entry per app you sell.
export const storeProducts = [
  {
    productId: "x32-sonicsniper",
    name: "X32 SonicSniper",
    blurb: "Real-time microphone feedback suppression for the Behringer X32.",
    plans: [
      { plan: "monthly", label: "Monthly", price: "$—/mo" },
      { plan: "lifetime", label: "Lifetime", price: "$—" },
    ],
  },
];
