package spawner

import "os"

// osEnviron is the production-mode wrapper around os.Environ. Split out
// so tests can swap it (currently unused but kept as a seam).
func osEnviron() []string { return os.Environ() }
