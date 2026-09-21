// Product catalog for the Simple Computers 101 Licensing System.
//
// ONE place to register every product you license. Both the storefront (the
// public "Buy" area) and the admin "Product" dropdown read from this list, so
// adding a product here makes it sellable and issuable everywhere at once.
//
// To add a product, copy a block below and set:
//   productId  must EXACTLY match that app's AppConfig.product_id (the app
//              rejects a license/token issued for a different product id).
//   name       shown to customers and in the admin dropdown.
//   blurb      one-line description on the storefront.
//   plans      ONLY for products sold online via Stripe -- each plan's real
//              price id lives in cloud/functions/pricing.py. Leave `plans: []`
//              for a product whose keys you issue by hand from the admin panel;
//              it then appears in the admin dropdown but not the storefront.
export const products = [
  {
    productId: "x32-sonicsniper",
    name: "X32 SonicSniper",
    blurb: "Real-time microphone feedback suppression for the Behringer X32.",
    plans: [
      { plan: "monthly", label: "Monthly", price: "$—/mo" },
      { plan: "lifetime", label: "Lifetime", price: "$—" },
    ],
  },

  // --- Add your next product here ---
  // {
  //   productId: "my-next-app",
  //   name: "My Next App",
  //   blurb: "Short description shown on the storefront.",
  //   plans: [],   // [] = not sold online yet; issue keys from the admin panel
  // },
];
