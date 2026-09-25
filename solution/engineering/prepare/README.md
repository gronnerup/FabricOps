Solution folder for prepare layer.

This layer contains items for data preparations and transformation, mainly Notebooks.

-----## Two things the notebooks learned the hard way

**The helpers `%run` has to be the first cell that executes.** A notebook attached to a
pipeline has its **parameters cell** overridden wholesale at run time, so anything you put
there is replaced by the pipeline's own parameters. Putting a helper call above the `%run`
means calling a function that has not been loaded. Order: parameters cell (declarations
only), then `%run` of the shared helpers, then the work.

**`notebookutils.credentials.getToken` takes `"pbi"`, not `"fabric"`.** The valid
audiences are `storage`, `pbi`, `keyvault` and `kusto`; anything else raises
`Py4JJavaError: "<x>" is not a valid resource`. `pbi` is the one that covers the Fabric
REST API, which is what the SQL analytics endpoint metadata refresh in the Curated notebook
calls.
