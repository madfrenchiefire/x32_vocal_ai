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
