// Firebase web config for the license portal (public values -- safe to ship).
export const firebaseConfig = {
  apiKey: "AIzaSyBl•••••••••••••••••••••••••••••••",
  authDomain: "x32-sonicsniper.firebaseapp.com",
  projectId: "x32-sonicsniper",
  storageBucket: "x32-sonicsniper.firebasestorage.app",
  messagingSenderId: "532383339989",
  appId: "1:532383339989:web:8edd1c47a4432190f6995c",
  measurementId: "G-BEG0SQ4BJF",
};

// Region the Cloud Functions are deployed to.
export const functionsRegion = "us-central1";

// Base URL for the HTTP functions (create_checkout_session lives here).
export const functionsBaseUrl = "https://us-central1-x32-sonicsniper.cloudfunctions.net";

// Products shown in the storefront. Prices are display-only labels; the real
// charge comes from the Stripe price ids in cloud/functions/pricing.py.
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