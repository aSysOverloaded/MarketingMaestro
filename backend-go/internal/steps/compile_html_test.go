package steps

import "testing"

func TestGetBrandConfigByModel(t *testing.T) {
	s := &CompileHTMLStep{}
	cases := map[string]string{
		"Bosch 800 Series Dishwasher":     "Premium Home", // used to be branded LG via the "wash" substring
		"Whirlpool Front Load Washer":     "Premium Home",
		"LG TurboWash Washing Machine":    "LG Electronics",
		"Samsung Family Hub Refrigerator": "Samsung",
		"Bulgari Espresso Machine":        "Premium Home", // contains "lg" but not as a word
	}
	for model, want := range cases {
		if got := s.getBrandConfigByModel(model).Name; got != want {
			t.Errorf("getBrandConfigByModel(%q) = %q, want %q", model, got, want)
		}
	}
}
