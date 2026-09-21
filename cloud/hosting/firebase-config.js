// Firebase web config for the license portal (public values -- safe to ship).
//
// Fill the REPLACE_ME fields from the sc101-licensing project's Web app:
//   Firebase console > Project settings > Your apps > Web app > SDK setup and
//   configuration (Config). The domains below are already correct for this
//   project; you only need apiKey, messagingSenderId, appId, measurementId.
export const firebaseConfig = {
  apiKey: "REPLACE_ME",
  authDomain: "sc101-licensing.firebaseapp.com",
  projectId: "sc101-licensing",
  storageBucket: "sc101-licensing.firebasestorage.app",
  messagingSenderId: "REPLACE_ME",
  appId: "REPLACE_ME",
  measurementId: "REPLACE_ME",
};

// Region the Cloud Functions are deployed to.
export const functionsRegion = "us-central1";

// Base URL for the HTTP functions (create_checkout_session lives here).
export const functionsBaseUrl = "https://us-central1-sc101-licensing.cloudfunctions.net";

// The product catalog moved to ./products.js -- register every product you
// license there (one place, read by both the storefront and the admin panel).
